---
parent: Decisions
nav_order: 15
title: "ADR-0015: A confirmed device deletion destroys the accessory — no orphan retention, no re-adopt"

status: "accepted"
date: 2026-08-26
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0015: A confirmed device deletion destroys the accessory — no orphan retention, no re-adopt

## Context and Problem Statement

Since issue #219 (ADR-0010), an endpoint that leaves the plugin's export list
is **orphaned**, not deleted: `endpoint-map.json` keeps its number, role and
label forever, so a re-adopt UI can hand the identity to a recreated device
later. #273 set out to measure what that retention costs and #274 asked
whether it is still worth it. Should a **confirmed** device deletion still
go through that same soft, indefinite-retention path, or should it destroy
the accessory outright?

### The evidence that motivated this turned out to be wrong, and that matters

#273 originally reported nine orphaned identities as **live published
endpoints** — duplicate names, dead accessories, inflated counts in every
paired ecosystem. That finding was **retracted on the same issue**: the
`root.parts.aggregator.parts.*` storage keys #273 read as "still published"
are matter.js's own leftover storage for endpoints that are no longer
instantiated, not evidence of one. A direct count at a bridge restart showed
57 endpoints instantiated against 57 exported identities — every orphan
absent, no duplicates in any ecosystem. **Orphaning an endpoint already
stops it being published; it always did.** The "nine zombie endpoints"
premise this ADR was expected to cite is false, and #274's own follow-up
comment says so in as many words: *"the original retention decision was
defensible after all: an orphan really is inert bookkeeping."*

So Option C from #274 (stop publishing orphaned endpoints) is moot — nothing
publishes them. This ADR is **not** about that. What survived the
retraction, and is the actual reason for this decision, is #274's second
question: **is the deleted-device re-adopt case worth what it costs to
support**, independent of whether an orphan is visible? Issue #246's
migrate — retargeting a *live* device onto an existing identity — already
covers the workflow people actually reach for. Re-adopt's remaining scope is
narrower: inheriting an identity onto a device that did not exist at the
moment the original was removed. That is real surface area — ADR-0010's
`publishedAs`/`deviceId` split, `orphaned`/`orphanedAt`/`supersededBy`
bookkeeping, the PR5 re-adopt dialogs, `list_orphans` — kept alive
indefinitely for a workflow with no measured cost to *not* having, once the
"it's actively harmful" premise is gone.

## Decision Drivers

* **Simplicity, once the cost argument for keeping it evaporated.** An orphan
  being "free" was the reason retention won the original #219 design debate;
  now that it is confirmed free either way, the question is purely whether
  the re-adopt machinery earns its keep, and #246 migrate already serves the
  workflow that matters.
* **A confirmed deletion is unambiguous.** Indigo tells the plugin a device
  no longer exists in `indigo.devices`. There is no future in which that
  specific device comes back — a "recreated" device is a NEW Indigo id from
  the moment it is recreated, which is precisely the case re-adopt exists
  for and precisely the case this ADR removes.
* **The destructive alternative must not punish an unrelated failure.** A
  device that merely fails classification this one pass (a dependent plugin
  not yet started, a role this build does not know) is not deleted — it must
  not be treated as if it were. Conflating "confirmed gone" with "could not
  classify this pass" would make a transient startup race permanently
  destroy an accessory, which is a strictly worse failure than the orphan
  being removed.

## Considered Options

* Destroy on confirmed deletion; leave `attach`'s ordinary reconcile
  untouched (chosen)
* Keep indefinite orphan retention for every departure (status quo)
* Destroy on ANY departure from the live set, including an ordinary `attach`
  reconcile that simply does not mention an identity this pass

## Decision Outcome

Chosen option: **destroy the endpoint-map record on a CONFIRMED departure,
leave everything else exactly as it was.**

"Confirmed" is deliberately narrow and is decided in exactly one place:
`export_bridge.py`'s `_spec_for`, the only code that can tell "the Indigo
device does not exist" (`device_getter` returns `None`) from every other
reason a device might not be sendable this pass. Three call sites now reach
the wire's `remove_endpoint` with a new `permanent: true` flag once that
existence check (or an equally unambiguous deliberate action) has been made:

1. `plugin.py`'s `deviceDeleted` — the device is gone, this instant.
2. `export_dialog_mixin.py`'s `exportRemove` — the user deliberately
   un-exported it. Kept alive only through "Migrate an exported accessory…"
   is the point: an un-export is as final as a deletion, not a temporary
   suspension.
3. `export_bridge.py`'s new `_purge_confirmed_deleted` (drained from
   `_on_attached`) — the gap #274 asked to close: a device deleted while the
   plugin was NOT RUNNING to hear `deviceDeleted` fire. The next attach's
   classify pass discovers it via the same existence check and destroys it
   the first moment the wire can carry the command.

`bridge-node`'s `EndpointMapStore.destroy()` is the new mutation these calls
reach: it deletes the record outright — no number, no role/label, no
`orphaned` marker — rather than `forget()`'s keep-everything-but-mark-it
behaviour. A destroyed identity is invisible to `restorable()` (nothing to
rebuild at the next node restart) and to `orphans()`/`list_orphans` (nothing
to offer a re-adopt picker), because there is no re-adopt to offer it to.

**What does NOT change: `attach`'s own reconcile.** A full reconcile
(`node.ts`'s `forgetRemoved`, driven by the plugin's desired set) cannot
tell "the plugin dropped this on purpose" from "this device exists but
failed classification this one pass" — that is exactly the ambiguity the
decision driver above rules out guessing at. So an identity that merely
falls out of an `attach`'s desired set keeps the pre-#274 soft/orphan
treatment, unconditionally, regardless of why. Destroying is only ever a
`remove_endpoint(permanent: true)` decision, made with the device's status
already confirmed on the plugin side — never inferred from an attach diff.

**This supersedes ADR-0010 IN PART**, specifically its "For AI agents" rule
*"DON'T: prune retired/orphaned endpoint-map records"* and the "Bad, because
retired records accumulate without limit… no pruning is implemented,
deliberately" consequence. ADR-0010's actual mechanism — `publishedAs` as a
plugin-owned field distinct from `indigoDeviceId`, and the generation-bump
supersession `replace()` uses for a role change — is **untouched**: that
machinery is orthogonal to device deletion, still runs through the SAME
soft, two-command `remove_endpoint`/`upsert_endpoint` sequence it always
did (see "Consequences" below), and this ADR does not reopen it. ADR-0011's
rekey mechanism is likewise unaffected — a rekey never removes an endpoint
at all.

**This does not (yet) remove the re-adopt UI or `list_orphans`.** That is a
separate, larger change (tracked as the PR-B half of #274's work) — deleting
a dialog and a wire command is a bigger blast radius than changing what one
flag does, and is better reviewed on its own. Until then, `list_orphans`
simply answers emptier than before: an ordinary un-export or a confirmed
deletion no longer produces an entry for it to list.

### Consequences

* Good, because a confirmed deletion or un-export no longer leaves anything
  behind to reason about, list, or accidentally re-adopt onto the wrong
  device.
* Good, because the destructive path is opt-in per call (`permanent`,
  defaulting `false`) and reached from exactly three call sites the plugin
  controls — `attach`'s reconcile, and `replace()`'s two-command
  supersede/re-adopt sequence, are structurally unable to trigger it.
* Good, because the number-reissue guarantee (ADR-0010's actual safety
  property) does not depend on this file at all: matter.js's own persisted
  allocation for the retired `Endpoint.id` is what prevents reuse, not our
  witness of it, so destroying our witness is safe.
* Bad, because the re-adopt picker (`list_orphans`, the PR5 dialogs) is now
  reachable only through the narrowing set of departures that still go
  through `attach`'s ordinary reconcile without ever being confirmed —
  functionally close to dead code until PR-B removes it outright. Left in
  place deliberately, to keep this change to a behaviour change rather than
  a UI removal.
* Neutral, because `mass_removal_refused` (§3.1) is untouched, and does not
  need to change: it guards `attach`'s reconcile, which this ADR does not
  touch, and every destructive call this ADR adds is a per-device
  `remove_endpoint` outside that guard's scope — exactly like the
  `deviceDeleted` path that already existed before this issue. Destroying
  rather than orphaning makes a wrongly-issued `permanent: true` more
  costly, not the mass-removal guard less relevant; nothing here gives a
  single bad actor a way to reach many devices at once that it did not
  already have.

### Confirmation

`bridge-node/test/endpoint-map.test.ts` (`EndpointMapStore.destroy`),
`bridge-node/test/reconcile.test.ts` (`parsePermanentRemoval`),
`bridge-node/test/restore.test.ts` (issue #274 describe block: the wire
round trip, the default-false regression guard, idempotency, malformed
input), `tests/test_export_bridge.py` (`TestConfirmedDeletedSweep`),
`tests/test_export_wiring.py` (`deviceDeleted`/`exportRemove` now assert
`permanent=True`), `tests/test_bridge_protocol_frames.py`/
`tests/test_bridge_client.py` (the new `remove_endpoint_permanent` golden
frame). `npm test` (bridge-node) and `pytest` (plugin, both Python 3.11 and
3.13) green; `pylint` unchanged.

## Pros and Cons of the Options

### Destroy on confirmed deletion; leave `attach`'s ordinary reconcile untouched (chosen)

* Good, because it needs no new way to tell "confirmed" from "not sure" at
  the point the destructive decision is made — that distinction already
  exists, exactly once, in `_spec_for`.
* Good, because it adds no new wire-level ambiguity: `remove_endpoint`
  already existed, `permanent` is additive and defaults to today's
  behaviour.
* Neutral, because it leaves the re-adopt UI pointing at a list that will
  usually be empty — acceptable as an interim state, not a final one.

### Keep indefinite orphan retention for every departure (status quo)

* Good, because it is what shipped and measured (harmlessly, per the
  retraction) — no regression risk.
* Bad, because it keeps ADR-0010's full re-adopt machinery alive
  indefinitely for a workflow #246 migrate already covers for the case that
  matters (a live replacement device), for no longer-current reason.

### Destroy on ANY departure from the live set, including an ordinary attach reconcile

* Good, because it would be the simplest single rule: nothing survives being
  un-exported, however it happens.
* Bad, because it collapses "the plugin confirmed this device is gone" and
  "this device exists but failed classification this one pass" into the
  same action — precisely the failure mode #274 raised as the reason NOT to
  do this. A dependent plugin's slow startup would then permanently destroy
  every accessory it drives, self-inflicted, on a bridge restart it had
  nothing to do with.

## More Information

Issues: [#273](https://github.com/simons-plugins/indigo-matter/issues/273)
(the retracted "nine zombie endpoints" measurement — read the whole thread,
not just the title), [#274](https://github.com/simons-plugins/indigo-matter/issues/274)
(this decision, including its own retraction and the surviving "is re-adopt
worth its machinery" question). Related, unaffected: ADR-0010 (superseded
IN PART — the `publishedAs` split and role-change generation bump stand),
ADR-0011 (rekey — orthogonal, never removes an endpoint). Protocol shapes:
[`../BRIDGE_PROTOCOL.md`](../BRIDGE_PROTOCOL.md) §3.3 (`remove_endpoint`'s
`permanent` flag), the `endpoint-map.json` subsection ("Destroying an
entry"), §3.12 (`list_orphans`).

## For AI agents
- DO: treat a confirmed device deletion or a deliberate un-export as
  PERMANENT — `remove_endpoint(permanent: true)`, `EndpointMapStore.destroy()`
  — never as something to retain for a future re-adopt.
- DO: keep the existence check (`device_getter(id) is None`) as the ONLY
  place that decides "confirmed gone" on the plugin side. Do not infer it
  from a classify failure, a role mismatch, or any other skip reason.
- DON'T: make `attach`'s ordinary reconcile (`node.ts`'s `forgetRemoved`
  without `hard: true`) destroy anything. It cannot tell a deliberate
  departure from a transient classify failure, and guessing wrong there
  destroys an accessory over a startup race.
- DON'T: add a "confirmed after N misses" or grace-period heuristic to paper
  over the point above. The existence check is definitive the first time it
  is made; there is nothing to wait for.
- DON'T: touch `mass_removal_refused` (§3.1) as part of this — it guards
  `attach`'s reconcile, which this ADR does not change.
- DON'T: reopen ADR-0010's `publishedAs`/`deviceId` split or its role-change
  generation-bump mechanism. Only the "never prune an orphan" consequence is
  superseded here.
