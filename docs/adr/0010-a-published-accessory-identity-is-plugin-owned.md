---
parent: Decisions
nav_order: 10
title: "ADR-0010: A published accessory identity is a plugin-owned, defaulted field"

status: "accepted"
date: 2026-08-18
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0010: A published accessory identity is a plugin-owned, defaulted field

## Context and Problem Statement

> Citation convention for this ADR: bare `F1`-`F9`, `E1`-`E12`, `§0`-`§9` and
> "owner ruling N" all refer to the PR5 design note named under
> [More Information](#more-information) — **not** to this repo's delivery
> epics, which reuse `E<N>` for something else entirely.

Two issues turn on the same fact: the string a Matter controller actually
remembers an accessory by. Today that string — `Endpoint.id`, the bridged
`UniqueID`, the bridged `SerialNumber`, and the `endpoint-map.json` key — is
one derivation, `indigo-<deviceId>`, appearing in four places.

* **#219 re-adopt.** A device is deleted and re-created (or the user simply
  re-exports it). The recreated device gets a NEW Indigo device id, so its
  accessory is a stranger to every paired ecosystem — a fresh room, a fresh
  name, no scenes, no automations — even though the OLD accessory identity is
  sitting untouched in the endpoint map, retained exactly so this could be
  avoided (BRIDGE_PROTOCOL §3.3).
* **#240 role change.** Changing an export's role has to change the Matter
  device type of a live endpoint, which §4.1 refuses outright
  (`role_change`). The existing fallback — recreate the endpoint IN PLACE, at
  the SAME number — sometimes works and sometimes leaves Apple Home stuck on
  "could not change settings" until the home hub is restarted, because a
  cached accessory structure changed under a number a controller believed was
  stable.

Both are the same question from opposite directions: can the string a
controller keys an accessory on ever legitimately differ from
`indigo-<deviceId>` — pointing a DIFFERENT device at an EXISTING identity
(re-adopt), or pointing the SAME device at a NEW identity (role change)? And
if so, who is allowed to make that happen, and how does it survive the next
full reconcile without being silently undone?

## Decision Drivers

* **§6.2 already settles who owns what.** The bridge node is the source of
  truth for an accessory's NUMBER; the plugin is the source of truth for
  WHICH accessories are exported and how. Whatever carries this decision must
  not blur that line in either direction.
* **`attach` is a full reconcile, not a delta.** Every (re)connect sends the
  plugin's ENTIRE desired set and the node reconciles the live set to match
  it (§3.1). Anything the node alone decided about an identity would be
  silently erased by the plugin's very next attach, which knows nothing
  about it.
* **Measured facts about matter.js 0.17.8 (§0 F1-F9) constrain the design,
  not just describe it.** The endpoint number is allocated from a monotonic
  counter keyed on `Endpoint.id` (F1) — there is no way to get a FRESH number
  without a fresh `Endpoint.id`, and no way to keep an EXISTING number except
  by declaring the SAME `Endpoint.id` again. The declared `UniqueID`/
  `SerialNumber` win over anything persisted (F8) — matter.js will not
  restore an old identity for us; whoever re-adopts has to declare it.
* **A retired number must never be reissued (F6).** matter.js *would* hand a
  freed number to a different accessory if asked explicitly — that is
  precisely the "two accessories swap identities" failure the drift detector
  exists to catch. The design must make that mistake structurally hard to
  make, not just documented against.

## Considered Options

* Plugin-owned, defaulted `publishedAs` field, sent on every attach/upsert
  (chosen)
* A dedicated `readopt_endpoint` bridge command
* The plugin edits `endpoint-map.json` directly
* Node-owned generation with an acknowledgement protocol

## Decision Outcome

Chosen option: **a first-class, plugin-owned, defaulted field**. The three
things today's `indigo-<deviceId>` derivation conflates —
`Endpoint.id`/`UniqueID`/`SerialNumber` on one side, and the Indigo device
driving them on the other — become two: `publishedAs` (what the accessory is
known as) and `indigoDeviceId` (what drives it). `ExportStore` persists
`publishedAs` per export; `_spec_for` sends it on every attach/upsert; the
node's job shrinks to exactly what §6.2 already gives it — allocating and
persisting the NUMBER behind whatever identity it is handed, never inventing
or retiring one on its own initiative.

`publishedIdFor(deviceId, generation)` is the one derivation both peers
implement identically: generation 1 is `indigo-<deviceId>`, byte-identical
to every export today (so nothing is orphaned by this change landing);
generation N≥2 is `indigo-<deviceId>~N`. **Re-adopt** sets
`publishedAs = orphan's old identity, indigoDeviceId = new device` — the
node sees a familiar `Endpoint.id` and F1 hands back the SAME number, so
nothing on the wire changes and every ecosystem keeps the room. **Role
change** sets `publishedAs = next generation of the same device's identity`
— the node sees a NEW `Endpoint.id` and F1 has no choice but to allocate a
fresh number, so the old identity is retired (never deleted — F4/F6) and
controllers process a clean removal-then-addition instead of a mutation
under a stable number.

The node's one new obligation is read-only: `list_orphans` (§3.12) answers
what the endpoint map already knows, so the plugin's re-adopt picker has
something to offer. It is not a recovery command — a node refusing to start
has no orphans it can vouch for — and it changes nothing.

### Consequences

* Good, because §6.2 is preserved exactly, not relaxed: the node still never
  decides which accessory identity a device gets, only what number that
  identity has.
* Good, because both #219 and #240 are the SAME mechanism from two directions
  — one field, two ways to move it — rather than two bespoke features that
  would each need their own version-skew story.
* Good, because generation 1 is byte-identical to today's derivation, so
  every existing export is unaffected until its role is deliberately changed
  or it is deliberately re-adopted.
* Good, because a retired identity is never reused (F6) — the endpoint map
  keeps every superseded/orphaned record forever, which is what makes the
  swapped-identity failure structurally hard to reach rather than merely
  discouraged.
* Bad, because retired records accumulate without limit (edge case E10). No
  pruning is implemented, deliberately (owner ruling 5) — the record is what
  documents a number is spoken for, and the observed growth rate (one row per
  role change per device) is noise at any realistic scale.
* Bad, because version skew is now a real hazard: an OLD bridge node given a
  `publishedAs` it does not understand would silently ignore it and publish
  the DEFAULT identity — a duplicate accessory, invisible until an ecosystem
  shows two of the same device. Resolved by bumping `PROTOCOL_VERSION` 1→2 in
  the SAME commit that first sends `publishedAs`, so an old node cannot get
  past the handshake with a v2-speaking plugin at all (owner ruling 1;
  BRIDGE_PROTOCOL §6.5 "fail-closed" applied as written, rather than adding a
  permanent `bridgeVersion` capability gate for a feature most exports never
  touch).

### Confirmation

`bridge-node/test/reconcile.test.ts`, `endpoint-map.test.ts`,
`registry.test.ts`, `protocol.test.ts`/`fixtures.test.ts`, `restore.test.ts`/
`node.test.ts` (published-identity defaults/validation, supersede-vs-recreate
planning, the F7/F8 UniqueID/SerialNumber pin, restore binding the recorded
`deviceId`). `tests/test_export_store.py`, `test_export_bridge.py`,
`test_export_menu.py`, `test_bridge_protocol*.py` on the plugin side
(persistence, the generation bump on a role change and its absence
elsewhere, the Re-adopt dialog's full validation order, the Python↔TypeScript
derivation pin). `npm test` (bridge-node) and `pytest` (plugin) both green.

## Pros and Cons of the Options

### Plugin-owned, defaulted `publishedAs` field (chosen)

* Good, because it fits inside §6.2 without adding a new ownership axis.
* Good, because `attach`'s full-reconcile semantics keep working unmodified —
  the plugin simply declares a different string, the node's existing
  create/update/remove logic already does the right thing per F1.
* Neutral, because the plugin now has to remember one more per-export field
  forever — but it already persists role/name/options per export, so this is
  additive to an existing store, not a new one.

### A dedicated `readopt_endpoint` command

* Good, because it reads as the most direct way to say "hand this identity to
  that device".
* Bad, because it cannot survive `attach`. The node has no way to persist a
  fact the plugin's very next full reconcile does not also assert — either
  the node ignores the plugin's post-restart declaration (breaking §6.2) or
  it echoes the mapping back for the plugin to persist anyway, which is this
  option with extra steps and an ack protocol on top.

### The plugin edits `endpoint-map.json` directly

* Good, because it needs no new command at all.
* Bad, because `EndpointMapStore` holds the map in memory and rewrites the
  whole file on every `persist()` while the node runs — a plugin-side edit is
  overwritten by the very next drift check. This is the same ownership
  boundary `identity.json` already sits behind, for the same reason.

### Node-owned generation with an acknowledgement protocol

* Good, because the node is where F1's allocation behaviour actually lives,
  so it could in principle decide the generation itself.
* Bad, because without an ack the plugin's next attach asks for the OLD
  identity again with the NEW role, the node supersedes it AGAIN, and the
  generation climbs on every attach forever — the ack protocol this would
  require is strictly more machinery than one field the plugin already knows
  how to persist.

## More Information

Design and measured facts: `.scratch-pr5-design.md` (workspace-local, not
committed) — §0 (F1-F9), §1.1-§1.3. Protocol shapes:
[`../BRIDGE_PROTOCOL.md`](../BRIDGE_PROTOCOL.md) §4.1/§4.3/§6.3/§6.5, the
`endpoint-map.json` subsection, and §3.12. User-facing walkthrough:
[`../MATTER.md`](../MATTER.md), "If you delete and recreate an exported
device". Issues: #219 (re-adopt), #240 (role-change supersede).

## For AI agents
- DO: treat `publishedAs` as the accessory identity and `indigoDeviceId` as
  only "who currently drives it" — the two are allowed to differ, and
  conflating them again reintroduces the bug this ADR exists to fix.
- DO: default `publishedAs` to `indigo-<deviceId>` (generation 1) everywhere
  a value is not explicitly stored — an entry, a spec, a summary. Generation
  1 must stay byte-identical to the pre-ADR-0010 derivation.
- DON'T: ever send an explicit endpoint `number` to the node (F6) — the node
  is the only writer of numbers, always via its own allocator.
- DON'T: add a node-side command that mutates `publishedAs` on its own
  initiative. Only the plugin bumps a generation or re-points an identity at
  a different device; the node only ever receives what `attach`/
  `upsert_endpoint` hand it.
- DON'T: prune retired/orphaned endpoint-map records. A reissued number is
  the one failure that duplicates accessories in every paired ecosystem —
  see owner ruling 5.
