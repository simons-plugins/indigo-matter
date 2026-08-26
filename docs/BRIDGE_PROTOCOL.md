# BRIDGE_PROTOCOL.md — plugin ⇄ bridge-node local protocol

**Version:** 2 (`protocolVersion: 2`)
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

## §0 — Measured probes (matter.js 0.17.8)

A handful of `bridge-node/src/` comments cite `§0` for a claim that is not
derivable from the matter.js typings or docs alone — it was established by
running the stack and reading back what it actually did. Recorded once here
rather than re-explained at every call site.

- **(a) The seeding trap.** An endpoint built WITHOUT `batPercentRemaining` in
  its initial `powerSource` state never gets attribute 12
  (`batPercentRemaining`) added to its `attributeList` at all — and a LATER
  `endpoint.set({powerSource: {batPercentRemaining: 48}})` still SUCCEEDS
  SILENTLY, reading back 48 on this process, even though no controller
  (Apple, Alexa, anyone) can ever read an attribute that was never declared.
  Mechanism: matter.js's `ValidatedElements` gates whether an *optional*
  attribute is enabled at all on `state[name] !== undefined` at construction
  time — `null` enables it (a real, declared value of "unknown"), an absent
  key does not. This is why `BATTERY_INITIAL` seeds `batPercentRemaining:
  null` unconditionally rather than leaving it out until a real reading
  arrives.
- **(b) Construction-time defaults.** `PowerSourceServer.with("Battery")`
  construction, without our supplying them, yields sane values for `status`
  (`0`), `order` (`0`), `description` (`"Battery power"`), `batReplaceability`
  (`0`) and `batReplacementNeeded` (`false`) — none of them ours to set.
  `initialize()` itself touches neither `order` nor `batReplacementNeeded`;
  their values arrive from the cluster's own schema defaults instead, not
  from behaviour code. The measured claim is narrower than it might look: it
  is that construction succeeds with usable values for the whole set, not
  that any one of them is set by a particular code path.
- **(c) Endpoint-number stability under a second `.with()`.** Chaining
  `.with(PowerSourceServer.with("Battery"))` onto a device type that has
  already been `.with()`-ed once (every role's own device type) retains the
  endpoint number across a close+re-add at the same `Endpoint.id`. It also
  adds `47` to `serverList` and `{deviceType: 17}` (`0x0011`) to
  `deviceTypeList`, via `PowerSourceServer#initialize`'s own
  `addDeviceTypes("PowerSource")`.
- **(d) `batPercentRemaining`'s own constraints.** Constrained to `0-200`
  (writing `201` throws a Matter `Constraint` error), nullable, and carries
  Matter's `Q` (quality) designation with a 10-second `minimumEmitInterval`
  on its `$Changed` — i.e. Matter itself, not this bridge, is what rate-limits
  how often a battery change reaches a controller.
- **(e) Endpoint numbers are allocated from a monotonic counter keyed on
  `Endpoint.id` (issues #219/#240).** The same `Endpoint.id` always gets the
  same number, every time it is (re-)added. A freed number is **never**
  automatically reused — `__nextNumber__` only ever climbs — and the
  reservation for a `close()`d id **survives a restart** while that id stays
  closed. This is the single fact that forces the whole `publishedAs` design:
  there is no way to get a *fresh* number without a fresh `Endpoint.id`
  (role-change supersede), and no way to keep an *existing* number except by
  declaring the *same* `Endpoint.id` again (re-adopt).
- **(f) `delete()` erases a number's reservation; `close()` does not (issues
  #219/#240).** This design never calls `delete()` — `close()` is the only
  removal verb, exactly as `registry.closeOne` already uses, because the
  whole point of retaining a number is that it comes back.
- **(g) `PartsList` reports a close+add as two distinct changes, even back to
  back (issues #219/#240).** A subscribing controller sees the shrunk
  `PartsList` as its own change notification before the grown one, with or
  without pacing in between — the same attribute read synchronously right
  after `close()` is stale for one microtask. `REMOVAL_PACING_MS` exists
  because of this, and the role-change supersede path inherits it for free
  because a full reconcile already does every removal (paced) before every
  create.
- **(h) An explicit `number` is honoured for a free number and refused for a
  live one (issues #219/#240).** matter.js *would* let us hand a freed number
  to a different accessory if we asked for it explicitly
  (`Endpoint <id> number <n> is allocated to another endpoint` for a live
  collision). That is a hazard, not a feature — it is precisely the "two
  accessories swap identities in every paired ecosystem" failure the drift
  detector exists to catch — so this design **never passes an explicit
  `number`**; every allocation goes through matter.js's own allocator.
- **(i) The only controller-visible identity is the endpoint number plus the
  bridged Basic Information attributes; `Endpoint.id` never reaches the wire
  (issues #219/#240).** Beyond the number, a controller can notice
  `UniqueID`, `SerialNumber`, `NodeLabel`, `ProductLabel`,
  `ConfigurationVersion` and the descriptor's `deviceTypeList` — all of it
  derived from `publishedAs`, never from `Endpoint.id` itself. Across a
  restart, matter.js **restores** a bridged accessory's
  `ConfigurationVersion` from storage, but it does **not** restore
  `UniqueID`/`SerialNumber` — whatever is declared at construction is what is
  published. A re-adopt therefore has to declare the OLD `publishedAs`
  itself; matter.js will not do it for us, and will not fight us either.
- **(j) `Endpoint.id` charset and `UniqueID` length (issues #219/#240).**
  `.` is refused outright (matter.js's own storage-key separator). `~` is
  accepted and appears **unescaped** in the storage key; `#` is accepted but
  percent-escaped. `~` is therefore the separator `publishedIdFor`'s
  generation suffix uses. `UniqueID` is capped at **32 characters** — the
  worst realistic published identity,
  `indigo-<Number.MAX_SAFE_INTEGER>~99`, is 26, comfortable, but
  `parsePublishedId`/`parse_published_id` enforce the cap explicitly rather
  than letting a caller discover it as an `AggregateError` mid-attach.
- **(k) matter.js already caps sessions per peer at 5, but too late to matter
  (issue #283 "Finding 2").** `SessionManager` (`SessionManager.ts:163`,
  `MAX_SESSIONS_PER_PEER = 5`) closes a peer's least-recently-active session
  once it opens a 6th (`#evictExcessSessionsFor`, force-close via
  `initiateForceClose({cause: new SessionEvictedError()})`, hooked off
  `#sessions.added`). It does not stop the report-routing defect (Finding 1
  below): the reference-server recurrence piled only **3** CASE sessions for
  one Echo peer inside 30 minutes — well under the built-in cap — before the
  routing defect started dropping reports. `session-hygiene.ts`'s superseded
  sweep is deliberately the same mechanism at a cap of **1**, not a
  duplicate of matter.js's own eviction.
- **(l) `Session`/`NodeSession` expose everything the sweep needs as public
  API, no matter.js patching required.** `Session.createdAt` (readonly,
  stamped at construction — `Session.ts:54`), `Session.activeTimestamp`
  (updated on every received message — `Session.ts:127-133`),
  `Session.initiateForceClose(context: {cause: Error; keepSubscriptions?:
  boolean})` (terminates the session and, unless `keepSubscriptions` is set,
  its subscriptions too — `Session.ts:266-287`) and `NodeSession.subscriptions`
  (a `BasicSet`, `.size` readable — already used by `churn.ts`'s wiring) are
  all public, all exactly what `SessionManager`'s own internal eviction (k)
  uses on itself.

  **`initiateForceClose` raises no session-teardown handshake with the
  peer, but it is not silent on the wire either — corrected here from an
  earlier version of this doc that claimed "no network send".** It closes
  the session's active `MessageExchange`s, and `MessageExchange#close` can
  still send a benign `StandaloneAck` for a message that was received but
  not yet acknowledged. There is no Matter close/goodbye exchange initiated
  with the peer — that is the accurate claim — but "no network send"
  overstated it.
- **(m) The wedge-watchdog signal (Finding 2 item 4) is NOT publicly
  observable — confirmed, not assumed.** `MessageExchange.onMessageReceived`
  calls `this.#notifyActivity(true)` (which sets `Session.activeTimestamp`)
  **unconditionally, before** it branches on `isStandaloneAck`
  (`MessageExchange.ts:411-432`): a bare MRP acknowledgement of a pushed
  report advances `activeTimestamp`/`isPeerActive` exactly as a genuine
  `SubscribeRequest`/`ReadRequest`/`InvokeRequest` would. The one counter
  that *would* separate "acked" from "consumed",
  `MessageExchange.#messageReceivedCounter`, is private with no getter, and
  is scoped to one exchange (a fresh one per IM transaction) rather than
  accumulated per session — there is no public vantage point from which to
  ask "how many real IM requests has this peer sent since T", only "when did
  it last acknowledge anything". This independently corroborates the HAMH
  maintainer's comment quoted in issue #283 ("an Apple controller can keep
  MRP-acking pushed reports … while it has stopped consuming data") — and is
  exactly why item 4 is not built (see `session-hygiene.ts`'s module
  docstring).
- **(n) A `ServerSubscription` cannot be rebound to a replacement session —
  age-based rotation is therefore restricted to subscription-free sessions.**
  `ServerSubscription.session` (`ServerSubscription.ts:207-209`) returns
  `this.#context.session`, a `readonly` field (`ServerSubscription.ts:131`)
  fixed once at construction and never reassigned anywhere in the class.
  Force-closing the session a live subscription is bound to — even with
  `keepSubscriptions: true`, which merely stops the *close* from also
  cancelling the subscription — leaves that subscription pointing at a
  session that no longer exists; nothing in matter.js re-attaches it to
  whatever session the peer opens next, so recovery depends entirely on the
  peer noticing (via its own keepalive/report timeout) and *choosing* to
  re-subscribe — which is not "clean re-subscribe", it is a self-inflicted
  instance of the exact staleness pattern #283 exists to fix. No live
  hardware was available to this probe to confirm what a real controller
  does when that happens, and the source reading above is sufficient reason
  not to risk finding out in production: `rotatableSessions` (below) only
  ever selects sessions with zero live subscriptions.
- **(o) Finding 1, confirmed directly in this repo's installed 0.17.8 (not
  taken on the HAMH maintainer's word alone).**
  `ServerSubscription.ts:830`'s `#sendUpdateMessage` calls
  `this.#context.initiateExchange(session ?? this.#peerAddress, …)` — a
  routine send passes no `session`, so routing falls through to
  `SessionManager.maybeSessionFor(peerAddress)`
  (`SessionManager.ts:543-559`), whose own comment states the policy
  outright: *"Prefer the most recently active session (i.e. the one we last
  heard from the peer on). Older ones may not work with broken peers."* — it
  picks by `activeTimestamp` recency, not by which session the subscription
  was actually created on. A peer holding a stale extra session that happens
  to be more recently *active* (m) than the session its subscription lives
  on will have reports routed to the session it isn't reading from.

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

The complete `error_code` domain for protocol version 2. Anything else is a
bug in the node.

| `error_code` | Meaning |
|---|---|
| `unknown_command` | Command name not in §3 |
| `malformed_args` | Args missing/mistyped for the command |
| `version_mismatch` | `attach` carried a different `protocolVersion`; node refuses and closes the socket after responding |
| `not_attached` | Any command other than `attach` before a successful `attach` on this connection |
| `unknown_device` | `indigoDeviceId` has no live endpoint |
| `unknown_role` | `role` not in the §4.2 enum |
| `role_change` | `upsert_endpoint` tried to change an existing endpoint's role, or to move it to a different `publishedAs` (§3.2/§4.1) |
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
   `{"protocolVersion": 2, "bridgeVersion": "<npm semver>", "matterJsVersion": "<semver>"}`
2. Plugin sends `attach` (§3.1). If `protocolVersion` differs from the
   plugin's own, the plugin does **not** attach: it surfaces an error naming
   what it needs — the bridge-node release paired with this plugin, installed
   from *Plugins ▸ Matter ▸ Install/update the Matter bridge* (typically the
   plugin was updated while launchd kept the old node alive; restarting the
   agent relaunches that same old node, so it is deliberately not the remedy
   the message gives). Node-side, a mismatched
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
  "protocolVersion": 2,
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

**The guard counts DEPARTURES, not removals** (issues #219/#240). A removed
`publishedAs` whose `indigoDeviceId` also appears among the identities being
created is one device changing which accessory identity it publishes — a
role-change supersession, or a re-adopt — and the device stays exported
throughout, so it is not a departure and does not count towards emptying the
live set. Without that carve-out, changing the role of the only exported
device on a bridge would be refused as a mass removal. Note this is
deliberately the BROAD "one removal plus one create for the same device"
rule, not the narrow generation test `supersededBy` uses: both shapes keep
the device exported, which is the only question the guard is asking.

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

**A driving-device change under a stable identity is a re-key, not a removal**
(issue #246, ADR-0011). If `endpoints` carries a spec whose `publishedAs` is
already live, whose `role` is unchanged and which does not gain a battery, but
whose `indigoDeviceId` names a DIFFERENT device from the one currently driving
that identity, the node re-keys rather than removes-and-recreates: the live
`Endpoint` — same `Endpoint.id`, same number, same clusters — simply starts
being driven by the new device, and `endpoint-map.json`'s record of who drives
it is updated on the next persist. No endpoint is added or removed:
`ConfigurationVersion` is not bumped and `PartsList` does not change, so no
paired ecosystem processes a removal or an addition — which is what room,
name and scene survival actually rests on. Label, reachability and state DO
land, as an ordinary subscribable attribute update (a value mismatch between
the old and new device is visible at migrate time, the same as any other
`set_state`) — only the identity's continuity is invisible to every
ecosystem, not every change the rekey carries. This is the mechanism behind
the plugin's "Migrate an exported accessory…" menu action — a live device
replaced by another one, or a module
moved to new hardware, while both Indigo devices exist at once — and it is why
migrate can honestly promise room/name/scene survival where the `list_orphans`
re-adopt path (whose identity DID lapse) cannot. A retarget combined with a
role change or a battery gain cannot be represented this way — a live cluster
set cannot be reshaped without being rebuilt — and falls back to the ordinary
`recreate` outcome above, wire activity and all; the plugin refuses cross-role
migrate at the dialog before it ever reaches here (#219/#240 reasoning).

### 3.2 `upsert_endpoint`

```json
{"command": "upsert_endpoint", "args": {"endpoint": <EndpointSpec>}}
```

Creates the endpoint if absent, updates label/reachable/state if present.
Idempotent. Result: `{"endpointNumber": <int>}`.

Two things it refuses with `error_code: "role_change"`, for one reason —
neither can be done to a live endpoint in place:

- a `role` different from the live endpoint's (§4.1: ecosystems cache the
  Matter device type per endpoint);
- a `publishedAs` different from the live endpoint's (issues #219/#240).
  That is a supersession or a re-adopt, and the only path allowed to do it is
  the plugin's remove-then-add, which sends the two mutations as two separate
  commands rather than asking this one to move a live endpoint out from under
  itself.

**A third refusal, the transpose of the second (issue #246, ADR-0011).** If
`endpoint.indigoDeviceId` is not itself live, but `endpoint.publishedAs` IS
live — on a DIFFERENT device — the node refuses with `error_code:
"role_change"` rather than creating a second endpoint under an `Endpoint.id`
the aggregator already holds. Falling through to an ordinary create here is
not a clean failure: matter.js raises an internal error on the duplicate id,
not a protocol refusal the plugin can act on. A driving-device change under a
stable identity is exactly the re-key outcome described in §3.1, and the only
peer allowed to cause it is `attach` doing a full reconcile — this refusal's
message names that path (the plugin's migrate flow) rather than leaving the
caller to guess why an apparently ordinary upsert failed.

### 3.3 `remove_endpoint`

```json
{"command": "remove_endpoint", "args": {"indigoDeviceId": 123456789}}
```

Removes the child endpoint (`endpoint.close()`). Idempotent — removing an
absent endpoint succeeds with `{"removed": false}`; a live removal returns
`{"removed": true}`. Bulk removals are paced ~100ms apart by the node.

**`permanent` (issue #274, default `false`) decides what happens to the
endpoint-map record, not just the live endpoint.** Omitted or `false` keeps
the behaviour every caller had before this flag existed: the persisted
endpoint-number allocation is **retained** and the entry is marked
`orphaned` (§4.3's `endpoint-map.json` subsection) — re-adding the same
device, or re-adopting the identity onto a different one, restores the same
number. `true` is the confirmed-gone declaration: the Indigo device has been
deleted, or the user has deliberately un-exported it. The node then
**destroys** the endpoint-map record outright — no number kept, no
`orphaned` entry, nothing offered to `list_orphans` — because ADR-0015
retired the premise that a departed accessory is worth holding open for a
re-adopt that will never come, once the driving device itself is gone.

Only a caller certain the device is not coming back may pass `true`. The
plugin passes it from exactly three places: `deviceDeleted` (a live
deletion), the export dialog's "Remove" (a deliberate un-export), and a
sweep in `_on_attached` that closes the one gap those two cannot — a device
deleted from Indigo while the plugin was not running to hear about it (§3.1
above already re-classifies every allow-list entry on each attach; a device
missing there is exactly this case, `export_bridge.py`'s `_spec_for`). The
plugin's own two-command role-change/re-adopt sequence (`replace()`, which
sends this `remove_endpoint` and a paired `upsert_endpoint` for the same or
a related identity) never passes `true` — that removal is deliberately soft,
because the very next command may retroactively mark it `supersededBy`
(§3.1's supersede accounting) or the map may still offer it as an ordinary
re-adopt candidate.

**`attach`'s own reconcile never destroys.** A full reconcile (§3.1) cannot
tell "the plugin dropped this on purpose" from "this device exists but
failed classification this one pass" — a dependent plugin not yet started,
a role this build does not know — so any identity that merely falls out of
an `attach`'s desired set keeps the pre-#274 soft/orphan treatment
regardless of `permanent`. Destroying is only ever a `remove_endpoint`
decision, made with the device's existence already confirmed on the plugin
side.

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

### 3.12 `list_orphans`

```json
{"command": "list_orphans", "args": {}}
```

Read-only, no arguments: every left-behind accessory identity the endpoint map
still remembers — issue #219's re-adopt picker is built from this. Result is a
bare array, `[<OrphanRecord>, …]`, in map order (the order numbers were first
recorded, not necessarily newest-orphan-first):

```json
[{"uniqueId": "indigo-123456789", "number": 7, "role": "dimmableLight",
  "label": "Kitchen Lamp", "orphanedAt": "2026-08-12T09:15:00Z", "deviceId": 123456789},
 {"uniqueId": "indigo-123456790", "number": 4}]
```

- `role`/`label` are absent for a pre-2026.16.2 orphan (that plugin version
  deleted them on un-export instead of recording them) — the picker lists it
  anyway, with nothing to match a replacement device against, and refuses to
  re-adopt it.
- `orphanedAt` is absent for an orphan recorded before this field existed — the
  picker renders that as "date unknown".
- `deviceId` is the Indigo device that drove the identity before it was
  un-exported, when recorded.
- A **superseded** identity (issue #240 — a role change moved its device to a
  new identity) is never in this list: re-adopting one would resurrect an
  old-role accessory under a number every paired ecosystem has already
  processed a removal for.

**Since issue #274 (ADR-0015), an ordinary departure no longer lands here at
all.** A device deletion or a deliberate un-export now goes through
`remove_endpoint`'s `permanent: true` (§3.3), which destroys the record
instead of orphaning it — see the `endpoint-map.json` subsection above. Only
a device that fell out of an `attach`'s desired set without the plugin ever
confirming why (§3.1's own reconcile never destroys) still produces an
ordinary orphan, so a healthy bridge's `list_orphans` is expected to answer
empty far more often than before this issue.

**Not a recovery command.** `list_orphans` requires an ordinary `attach` first,
same as any other §3 command — it is deliberately absent from §1.1's recovery
trio (`get_status`, `get_pairing`, `rebuild_endpoint_map`): a node refusing
over an unreadable map has no orphans it can vouch for.

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

- `indigoDeviceId` — the immutable Indigo device ID: the device this
  accessory is **driven by** — every §5 command is addressed to it and every
  §3.4 state push arrives keyed on it. It is no longer the accessory's
  identity (ADR-0010, issues #219/#240): `publishedAs` below is what
  `Endpoint.id`/`UniqueID`/`SerialNumber` are derived from, and it defaults to
  `indigo-<indigoDeviceId>` — which is why an ordinary export looks exactly as
  it always has, and why a re-adopted one has the two deliberately disagree.
- `role` — one of the v1 role enum (§4.2). A role change for an existing
  endpoint is **rejected** (`error_code: "role_change"`); the plugin must
  remove and re-add, because ecosystems cache device types per endpoint.
- `label` — Bridged Device Basic Information `NodeLabel`.
- `reachable` — Bridged Device Basic Information `Reachable`. **Omitting it
  means `true`.** An absent flag says nothing about availability, and the other
  reading — an accessory that greys itself out in every ecosystem because the
  plugin left a field off — is the worse default by far.
- `options` — role-specific extras (e.g. window-covering polarity). For
  `colorTemperatureLight`/`extendedColorLight` (issue #293), `options` MAY
  carry `ctMinMireds`/`ctMaxMireds` (ints) — the endpoint's physical
  `ColorTempPhysicalMinMireds`/`MaxMireds`. **Usable iff BOTH keys are
  present, both are integers (a JSON number with an integral value; a
  boolean is NOT an integer), and `153 <= ctMinMireds < ctMaxMireds <=
  500`.** Anything else (one key present without the other, a non-integer, a
  pair outside that inequality) is treated exactly as absent — never a
  refusal; a refused attach takes every export offline over one bad pair —
  with one warn log naming the device. When usable: on **construction**
  (create/recreate) the node sets `colorTempPhysicalMinMireds`/
  `MaxMireds` and `coupleColorTempToLevelMinMireds` to the declared pair,
  and clamps the initial `colorTemperatureMireds` (from `states`, default
  `MIREDS_MIN`) into it. On an **update** to a live endpoint (`upsert_
  endpoint`, §3.2) whose desired bounds differ from the live ones, the node
  applies them to the ColorControl state via `endpoint.set()`, clamps the
  endpoint's current `colorTemperatureMireds` into the new bounds in the
  same patch, and bumps BridgedDeviceBasicInformation `ConfigurationVersion`
  (`increaseConfigurationVersion`) so capability-caching ecosystems re-read
  the new physical range. Absent/unusable behaves byte-for-byte as today
  (static 153/500); an endpoint restored from the map with no plugin
  attached keeps the generic bounds until the next attach's update applies
  them. The plugin never sends its own SEED/LEARNED bookkeeping keys on the
  wire, only this effective pair, and only when it differs from the generic
  153/500 domain — see `ct_bounds.wire_options` in the plugin source.
- `publishedAs` — issues #219/#240. The accessory identity this device
  publishes as (`Endpoint.id`/`UniqueID`/`SerialNumber`) — **the plugin owns
  and persists it; the node owns only the number that identity gets** (§0
  (e)-(j), invariant 3). **Omitted (or equal to `indigo-<indigoDeviceId>`)
  means "use today's default derivation"** — every export that has never
  changed role sends no key at all, so this field is invisible on the wire
  until a role change or a re-adopt moves an accessory off its default
  identity. **Validation:** must match `indigo-<safeInt>[~<generation≥2>]`
  and be ≤32 characters (§0 (j)); anything else is refused with
  `malformed_args`. **Duplicate refusal:** `attach` rejects, also with
  `malformed_args`, an endpoint list that names the same `publishedAs` twice
  — this is PR5 design edge case E12's backstop, the one that actually matters, because
  it is what stops a hand-edited `.indiPref` pointing two Indigo devices at
  one accessory from ever reaching matter.js.
- `battery` — issue #220. `true` means "ensure this accessory publishes
  PowerSource"; **omitted or `false` means "no evidence right now", never a
  removal request.** The live cluster set is monotonic: once an endpoint is
  built with PowerSource, the node never takes it away, because removing it
  would need a recreate — the same accessory-identity churn a role change
  costs every paired ecosystem — for a trigger as weak as a device going
  momentarily silent about its battery. Mirrors the inbound side's add-only
  `SupportsBatteryLevel` policy. `battery: false` is never written on the
  wire (`to_wire()` omits the key entirely); a client that once said `true`
  for a device has no way to take it back, by design.

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
| `waterLeakDetector` | Water Leak Detector (0x0043) | `leak: bool` (true = leak detected) | — |
| `waterFreezeDetector` | Water Freeze Detector (0x0041) | `freeze: bool` (true = freeze detected) | — |
| `rainSensor` | Rain Sensor (0x0044) | `rain: bool` (true = rain detected) | — |
| `smokeAlarm` | Smoke CO Alarm (0x0076), SmokeAlarm feature | `smoke: bool` (true = alarming) | — |
| `coAlarm` | Smoke CO Alarm (0x0076), CoAlarm feature | `co: bool` (true = alarming) | — |
| `temperatureSensor` | Temperature Sensor | `temperatureC: float` | — |
| `humiditySensor` | Humidity Sensor | `humidityPct: float` | — |
| `lightSensor` | Light Sensor | `lux: float` | — |
| `pressureSensor` | Pressure Sensor | `pressureKPa: float` | — |
| `flowSensor` | Flow Sensor | `flowM3h: float` | — |
| `thermostat` | Thermostat | `localTemperatureC: float`, `heatingSetpointC: float`, `coolingSetpointC: float`, `systemMode: str` | `setHeatingSetpoint {"valueC": float}`, `setCoolingSetpoint {"valueC": float}`, `setSystemMode {"mode": str}` |

- **`smokeAlarm` and `coAlarm` are two roles over one Matter device type, and
  the node writes an attribute the plugin never sends.** Smoke CO Alarm 0x0076
  selects its sensing half by cluster feature; an Indigo sensor is one boolean
  meaning one thing, so exporting a smoke-only sensor with both features would
  have it reporting a CO reading nothing measured. The cluster's mandatory
  `expressedState` is **derived by the node** from whichever key it was given
  (`Critical` → the matching expressed state, otherwise `Normal`): the
  specification requires the two to agree, and Indigo carries nothing to derive
  `expressedState` from, so it is deliberately absent from this role's state
  keys rather than left to the plugin. Severity collapses to Normal/Critical —
  Matter's middle `Warning` tier has no Indigo counterpart. The cluster's one
  command, `SelfTestRequest`, has conformance `O` and is not declared, so these
  roles stay read-only like every other sensor.
- **The leak family and `contactSensor` share one Matter cluster and disagree
  about its polarity, on purpose.** All four carry BooleanState's `stateValue`,
  but the specification reads it as "true = closed or contact" in a Contact
  Sensor and as "true = detected" in each detector device type. Each role
  therefore gets its own state key rather than a shared `detected`: a role
  change re-creates the accessory (§3.6), and a shared key would let a state
  pushed under one reading be applied under the other.
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
- Every command in this table is emitted per ecosystem *invocation*, not per
  attribute change — `lock`/`unlock` and `goToPosition`/`stopMotion` always
  worked this way; as of #143, `onOff`, `setLevel`, `setColorTemp` and
  `setColor` do too. A command that matches the accessory's current state is
  still forwarded (on / off / toggle / onWithTimedOff / offWithEffect /
  onWithRecallGlobalScene all report even when nothing here moved), the same
  way `lock`/`unlock` already did. There is no longer an attribute-driven
  producer anywhere in the light/plug family: LevelControl's onOff coupling
  (a dimmer `*WithOnOff` command) used to write the OnOff attribute directly
  and reach Indigo through an attribute watcher; #143 converted that command
  too, so it now reports through the same per-invocation path as everything
  else in this row. A `*WithOnOff` command's frames depend on direction
  (issue #239): to the minLevel boundary it emits `setLevel {level: 0}` then
  `onOff {value: false}`; to any other level it emits `setLevel` ALONE —
  Indigo's own turn-on-to-level semantics do the turn-on, and a leading
  `onOff {value: true}` was live-observed racing the level command's Z-Wave
  reports and landing Indigo's brightness state at a stale 0. The watchers that used to be the producers for `onOff`/
  `setLevel`/`setColorTemp`/`setColor` are still wired, but only as
  backstops — a regression detector in case a future matter.js version
  starts writing one of these attributes again in a remote context, not the
  normal path.
- `setColor`'s hue/saturation is the only colour vocabulary in v1 — there is
  no CIE xy command. An ecosystem that drives an `extendedColorLight` in its
  xy representation (`moveToColor`) still reports as `setColor`, converted
  through the ColorControl cluster's own hue/saturation conversion
  (`xyToHsv`), so Indigo never has to speak a colour space §4.2 does not
  define.
- **`colorTemperatureLight`/`extendedColorLight` physical bounds (issue
  #293).** `colorTempMireds`'s domain is the generic 153-500 UNLESS the
  endpoint's `options` carry a usable `ctMinMireds`/`ctMaxMireds` pair (§4.1),
  in which case the node declares and clamps to that narrower, honest range
  instead — see §4.1 for the full construction/update behaviour. The plugin
  populates this from a learner that watches confirmed commanded writes
  against the device's own echo, not from a declared value taken as fact.
- **`extendedColorLight`'s `set_state` carries exactly ONE colour channel per
  push — `colorTempMireds` XOR `hue`/`saturation`, never both** (issue #282).
  The plugin decides which from the Indigo device's own colour mode; the
  node's `colorPatch` derives the endpoint's Matter `colorMode` from key
  presence in that same push, preferring hue/saturation only when the push
  carries it. "CT alone ⇒ `ColorTemperatureMireds`" is therefore a producer
  **contract** this table records, not an accident of today's plugin
  behaviour — a future producer sending both keys in one push re-introduces
  the coherence bug #282 fixed on the plugin side.
- This table is the complete producer contract, so it is worth naming what
  is deliberately absent from it. Commands that yield **no** `command`
  frame: continuous `moveHue`/`moveSaturation`/`moveColor` (no target value
  — nothing happens at the cluster level, so nothing is reported, matching
  stock); and `stepColor` (writes silently in stock, so it is a debug-logged
  drop rather than a reported command — CIE xy step commands are not yet
  converted). Commands against an accessory the node believes is off are
  **forwarded, not gated** — level AND colour alike (issue #235: the bridge
  sends, Indigo decides; both clusters seed `executeIfOff: true` because the
  stock gate reads the `onOff` attribute, which under no-auto-confirm means
  "Indigo's last confirmation" rather than the device's state). Indigo's own
  semantics then apply: brightness-while-off is turn-on-to-that-level, a
  colour write does whatever the device's driver does with one, and a
  colour-temperature change on a lamp with a white channel is normally
  stored without turning the lamp on (`_set_color_temp`'s own documented
  choice; an RGB-only lamp refuses it with a logged reason, and a driver
  that retains a non-zero `whiteLevel` while off will light up).
- `batteryLevel: 1-100` (issue #220) is the first **role-independent**
  `set_state` key — valid for `set_state` against ANY role whose endpoint was
  built with `battery: true` (§4.1), not listed against any single role above.
  The node owns the 0–200 half-percent `batPercentRemaining` conversion
  (24% → 48). Sending it against an endpoint built without `battery: true` is
  refused with `malformed_args` — a silent accept would write a PowerSource
  attribute no controller could ever read, since the cluster does not exist
  on that endpoint at all.

### 4.3 `StatusReport` and `FabricInfo`

```json
{"commissioned": true,
 "fabrics": [{"fabricIndex": 1, "label": "Apple Home", "vendorId": 4937}],
 "endpointCount": 12,
 "endpoints": [{"indigoDeviceId": 123456789, "endpointNumber": 2, "role": "onOffLight",
                "publishedAs": "indigo-123456789"}],
 "drift": [],
 "driftChecked": false,
 "warnings": [],
 "subscriptionChurn": {"checked": true, "active": false, "peers": []},
 "sessionHygiene": {"checked": true, "peers": [], "closed": {"superseded": 0, "dead": 0, "rotated": 0}}}
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
- `warnings` — faults the node has hit and cannot fix on its own, in prose. Most
  are persistence failures: the endpoint map could not be written or deleted,
  the commissioning witness could not be cleared, `identity.json` could not be
  saved. Since bridge-node 0.15.0 the channel also carries two things that are
  not writes at all — the issues #219/#240 re-adopt notices, and the issue #286
  subscription-churn notice described below — because they are the same kind of
  thing: a fault a human has to act on that appears nowhere else. Empty is the
  normal state, and entries are **current, not historical** — a warning
  disappears the moment the condition it describes ends.

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

- `subscriptionChurn` — whether a *controller* is churning its subscriptions
  against this bridge (issues #283/#286). Added in bridge-node 0.15.0 with **no
  `protocolVersion` bump**, the same additive precedent `driftChecked` and
  `warnings` set: a client that does not know the field ignores it, and a client
  that does must tolerate its absence from an older node by defaulting to
  `{"checked": false, "active": false, "peers": []}`.

  ```json
  {"checked": true,
   "active": true,
   "peers": [{"peerNodeId": "41869fbd537ef01", "fabricIndex": 2, "liveSessions": 5,
              "invalidDeletions": 3, "windowMinutes": 30,
              "since": "2026-08-23T09:12:00.000Z"}]}
  ```

  - `checked` — whether the node could observe its own session layer at all.
    **`false` is not the healthy answer.** It means the session observables
    could not be wired, or a handler failed and the detector was switched off
    rather than left reporting a tally it knows is short; `active: false` beside
    it says only that nothing was seen, by something that was not looking. Gate
    on `checked` exactly as you gate on `driftChecked` before reading `drift`.

    The two ways of reaching `false` are told apart on the `warnings` channel,
    because the advice differs. A detector that **worked and then failed** keeps
    a warning up (`Subscription-churn detection FAILED …`): whatever churn it
    was reporting has not stopped, only become invisible, and a restart fixes
    both — withdrawing the notice exactly when detection died would take away
    the one actionable thing the user had. Wiring that **never attached** stays
    warning-free: nothing was ever reported, so there is nothing to withdraw,
    and a restart would fail identically every time — a permanent un-actionable
    warning is how a channel gets ignored. `checked: false` is the honest signal
    in both cases.
  - `active` — at least one peer is currently over a threshold.
  - `peers` — the over-threshold peers, and only those; empty whenever `active`
    is `false`. Peers are listed individually because one fabric holds several
    and they are not interchangeable: two Echoes on one Alexa fabric churn
    independently, and the user is being asked to act.
  - `since` is when that peer FIRST crossed a threshold in the current episode,
    not when it was last seen churning. It holds still while the churn persists
    and restarts only after a genuine recovery.

  The fault, measured on the reference server (issue #283, 2026-08-23): Alexa
  opens a CASE session, subscribes, reports the subscription invalid some
  minutes later, and re-subscribes over a *new* session while the old ones are
  never reaped — three generations for one Echo peer inside 30 minutes, a
  ~30-minute cycle, 24 SubscribeRequests a day.

  The node detects it from the **rate of terminated-subscription deletions per
  peer** *together with* that peer's **live CASE session count**, never from a
  dependency's log text. The conjunction is load-bearing, not caution.
  `Subscription.isTerminated` is not a synonym for "the peer reported it
  invalid": in matter.js 0.17.8 it is set by three unrelated things, and two of
  them are innocent — `handlePeerCancel()`, which fires on **every** existing
  subscription whenever a peer re-subscribes with `keepSubscriptions: false`
  (the routine path for Apple, Alexa and Google), and the give-up branch after
  three failed updates on a transient network error (a Wi-Fi blip). A deletion
  count on its own would therefore tell the owner of a perfectly healthy
  controller to restart a working bridge. What separates the fault is that the
  deletions land on a peer whose **sessions are also piling up**: a healthy
  controller re-subscribes over the session it already holds, or closes the old
  one; the #283 peer does neither. So deletions count only against a peer
  holding at least two live sessions, while a large enough pile (four) is the
  fault on its own however it arose.

  Detection is read-only: the node counts and reports, and closes nothing. The
  only recovery is a bridge restart, which is what the notice says.

  An active verdict also raises a `warnings` entry naming the peer, so a client
  that reads only the prose channel is still told. Both are cleared together
  when the fabric goes stable — the rolling window drains, or the piled-up
  sessions are reaped.

- `sessionHygiene` — app-level CASE session hygiene against this bridge
  (issue #283 "Finding 2"). Added in bridge-node 0.17.0 with **no
  `protocolVersion` bump**, the same additive precedent `subscriptionChurn`
  set: a client that does not know the field ignores it, and a client that
  does must tolerate its absence from an older node by defaulting to
  `{"checked": false, "peers": [], "closed": {"superseded": 0, "dead": 0, "rotated": 0}}`.

  ```json
  {"checked": true,
   "peers": [{"peerNodeId": "41869fbd537ef01", "fabricIndex": 2, "liveSessions": 2}],
   "closed": {"superseded": 4, "dead": 1, "rotated": 0}}
  ```

  Unlike `subscriptionChurn`, this feature **acts** — it does not merely
  report — through matter.js's public session-layer API, no dependency
  patching (§0(k)/(l); see `bridge-node/src/session-hygiene.ts` for the full
  design and its citations):

  1. **Superseded-session sweep** — the moment a peer opens a new CASE
     session, its OLDER ones are closed immediately. This is the core
     mitigation: it removes the pile-up precondition for the still-open
     matter.js report-routing defect (§0(o)) at a cap of ONE session per
     peer, well below matter.js's own built-in `MAX_SESSIONS_PER_PEER`
     eviction of five (§0(k)), which the reference-server recurrence proved
     too high to prevent the defect from biting.

     **Closes a superseded session even while it still holds a live
     subscription — deliberately**, the opposite rule from items 2/3 below.
     Orphaning the old session's subscription is the point, not a risk to
     mitigate: the peer that superseded it has, by definition, a NEW session
     already open, and the HAMH-proven behaviour is that it re-subscribes
     over that one. Items 2/3 have no such replacement in hand for the
     session they close — stranding a subscription there, with nothing for
     the controller to fall back to, would be the exact staleness pattern
     this mechanism exists to prevent. Same bet, opposite session, hence the
     opposite rule.
  2. **Dead-session force-close** — a CASE session holding zero
     subscriptions, quiet for 60s, is closed.
  3. **Age-based rotation** — a subscription-FREE CASE session older than 4h
     is closed. Deliberately narrower than closing any old session: §0(n)
     establishes that a `ServerSubscription` is bound for life to the
     session it was created on, so rotating a session under a live
     subscription would strand it, not migrate it.
  4. A fourth mechanism (a "wedge watchdog" for a controller that keeps
     MRP-acking pushed reports while it has stopped issuing new requests) was
     assessed and **not built** — §0(m) traces exactly which matter.js
     internal would need to become public for it to be possible, and it is
     not, today.

  - `checked` — same discipline as `subscriptionChurn.checked`: `false`
    means the hygiene machinery could not observe/act on the session layer
    at all (wiring never attached, or a handler failed and hygiene switched
    itself off), not "nothing needed closing". No `warnings` entry
    accompanies a `false` here, unlike churn — nothing hygiene does is a
    fault a user must act on, only a mitigation that has gone quiet.
  - `peers` — issue #283's own "diagnostic to run first when staleness
    recurs" (count live CASE sessions per peer), for EVERY peer holding at
    least one live CASE session, not only ones over a churn threshold — a
    standing view of a pile *forming*, complementing
    `subscriptionChurn.peers`' "act now" view.
  - `closed` — cumulative sessions this node has force-closed since it
    started, by reason. Never decreases within a run.

  Every action is logged once (the session that closed, which peer, why, and
  its age/quiet-time/session-count at the moment of closure) — never
  silently.

`endpoints[].role` is one of the §4.2 enum; `endpoints[].publishedAs` is
issues #219/#240's accessory identity (§4.1) — informational here, tolerantly
defaulted so a report from a pre-`publishedAs` node still parses;
`endpointCount` is `endpoints.length` (it is sent explicitly so a client can
log the size without walking the list).

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
  `false`. Set by an
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
- `battery` — issue #220, present (`true`) and absent otherwise, never
  `false`. Set the first time a *live* endpoint is recorded with PowerSource
  (mirroring §4.1's own `battery` flag) and never cleared afterwards — the
  monotonic wire rule applies to the witness too, or a restart would rebuild
  an accessory without the cluster it actually has. Purpose: restore-on-start
  (below) rebuilds the accessory with the same cluster set it had. Same
  inert-extra-key parse tolerance as `numberVoid`/`orphaned`. **Rollback
  caveat**: an older bridge that has never heard of this key drops it on its
  next write and restores the accessory without PowerSource on the next
  start — a visible cluster-set change in every paired ecosystem, which
  self-heals the moment a newer bridge (or the plugin's next `attach`)
  recreates the endpoint with `battery: true` again.
- `deviceId` — issues #219/#240, the Indigo device *currently driving* this
  published identity. Absent means "the key's own derivation" — every
  pre-`publishedAs` record, and every entry that has never been re-adopted.
  **Unlike `battery`, this is replace-on-change, not add-only** — a re-adopt
  is precisely a change of driving device, and add-only-forever would pin
  the deleted device's id in the record permanently. `restorable()` resolves
  an entry's device id from this field first, falling back to parsing it out
  of the `publishedAs` key itself (§4.1) — without it, a restore-on-start
  after a re-adopt would rebuild the accessory bound to the OLD, deleted
  device rather than the new one. Same inert-extra-key parse tolerance as
  the keys above. **Rollback caveat:** an older bridge drops the key on its
  next write and falls back to deriving the device id from the key itself —
  which is the *previous* driving device, not the re-adopted one — so a
  re-adopted accessory restored by a rolled-back bridge rebuilds bound to
  whichever device the key's own `indigo-<id>` names, self-healing the next
  time a `publishedAs`-aware plugin attaches and records the real driver.
- `orphanedAt` — issues #219/#240, an ISO-8601 stamp written by an un-export
  alongside `orphaned`, from the node's own injected clock. Purely
  informational — the re-adopt picker's "un-exported on …" column; absent on
  every orphan recorded before this field existed, which the picker renders
  as "date unknown" rather than guessing. Same inert-extra-key parse
  tolerance. **Rollback caveat:** an older bridge drops the key on its next
  write; the orphan is unaffected (still re-adoptable, still keeps its
  number), it simply reads as "date unknown" in the picker from then on —
  there is no self-heal because there is nothing to heal, the date was never
  load-bearing.
- `supersededBy` — issue #240, the published identity that replaced this one
  on a role change. Set when the node observes a removal paired with a create
  of a **later generation of that same identity**
  (`indigo-<id>` → `indigo-<id>~2`), whether the two land in one mutation
  (`forgetRemoved`'s pairing logic) or arrive as separate commands from the
  plugin's `replace()` (`noteSupersessionIfPaired`'s, called by
  `upsertEndpoint` once a create has landed). The generation test is the whole
  test, deliberately: a **re-adopt** onto an already-exported device is also
  one removal plus one create for one device, and the identity it leaves
  behind is an ORDINARY orphan that stays re-adoptable — only a generation
  bump retires an identity. A superseded record is `orphaned` too (it
  is not live) but is **excluded from both `restorable()` and
  `orphans()`/§3.12** — restoring it would resurrect an old-role accessory
  under a number every paired ecosystem has already processed a removal for,
  and re-adopting it would do the same thing by hand. Same inert-extra-key
  parse tolerance. **Rollback caveat:** an older bridge build drops the key
  on its next write, and the superseded record becomes an ordinary orphan —
  visible in `list_orphans` and re-adoptable, which would resurrect the
  retired role under its retired number if acted on. Reaching this state at
  all needs an out-of-band swap (a hand-installed older bridge-node build
  pointed at storage a newer one already wrote to) rather than an ordinary
  reconnect: invariant 5's fail-closed already refuses any *live* pairing
  whose `protocolVersion`s disagree, and the ordinary install/reinstall menus
  always fetch the pinned version. Documented here rather than defended
  against in code, for the same reason `numberVoid`'s and `orphaned`'s own
  rollback paths are not.

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

**Destroying an entry (issue #274, ADR-0015) is the alternative to
orphaning it — number, role, label and every other field, gone.** A
`remove_endpoint` carrying `permanent: true` (§3.3) — the driving device is
confirmed deleted, or deliberately un-exported — deletes the record from
`endpoints` outright rather than marking it `orphaned`. Unlike every mutation
above, this is the one place a record's `number` is not kept: the guarantee
that a retired number is never reissued (ADR-0010, `numberVoid`'s and
`orphaned`'s own comments above) still holds, because it was never this file
that enforced it — matter.js's own persisted allocation for the retired
`Endpoint.id`, a separate store this file never touches, is what actually
prevents reuse, and it is untouched by deleting this witness of it. What is
lost is re-adopt: a destroyed identity is invisible to both `restorable()`
and `orphans()`/§3.12 from the moment it is destroyed — there is nothing left
to rebuild or to offer the picker. `attach`'s own reconcile never destroys
(see §3.1 and §3.3 above); only an explicit `remove_endpoint` with
`permanent: true` does.

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
3. **Identity flows one way:** `publishedAs` → `Endpoint.id` → persisted
   endpoint number, and `publishedAs` defaults to `indigo-<indigoDeviceId>`
   (issues #219/#240) — a narrowing of the earlier
   `indigoDeviceId` → `Endpoint.id` → number statement, not a reversal:
   nothing is still ever keyed on list position or label, and every export
   that has never role-changed or been re-adopted publishes under exactly
   today's derivation.
4. **State pushes are echo-guarded** in the node (`ctx.offline`); the plugin
   never receives a `command` event for a change it pushed.
5. **Version skew fails closed:** mismatched `protocolVersion` means no
   attach (both peers enforce it), an error in the Indigo log, and untouched
   pairings. Applied literally, not just stated, for `publishedAs`
   (issues #219/#240, PR5 design owner ruling 1): `PROTOCOL_VERSION` bumped 1 → 2 in the
   same commit that first sends it, rather than adding a permanent
   `bridgeVersion` capability gate around it — an old (pre-#219/#240) node
   cannot silently ignore a `publishedAs` it does not understand and publish
   a duplicate default-identity accessory, because it never gets past this
   handshake with a v2-speaking plugin at all.
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
