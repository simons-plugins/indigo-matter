---
parent: Decisions
nav_order: 11
title: "ADR-0011: A driving-device change under a stable identity is a reconcile rekey"

status: "accepted"
date: 2026-08-18
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0011: A driving-device change under a stable identity is a reconcile rekey

## Context and Problem Statement

ADR-0010 split an export into two facts: `publishedAs` (what the accessory is
known as) and `indigoDeviceId` (which Indigo device drives it). It also proved
the two are allowed to differ — that is what re-adopt is. But it only exercised
the split for an identity that had already **lapsed**: the orphan flow hands a
left-behind identity to a new device, and the 2026-08-18 jarvis validation of
PR #244 measured what that lapse costs. Apple purges a removed accessory's
room, name and scene associations within seconds-to-minutes of processing the
removal — faster than a human can complete the orphan dialog — so the orphan
path restores wire identity (number, no duplicates, correct routing) but the
returned accessory lands in the Default Room under a default name.

The dominant real-world flow (issue #246) never needs the lapse at all: a
logical device migrating to new hardware — a socket replaced, a module moved
from zigbee to Matter — with **both Indigo devices existing at once**. The
identity, number and role are unchanged; only the driving device moves. In
principle that needs zero wire activity, and an identity that never lapses is
followed by every ecosystem with room, name, scenes and automations intact
(the in-transaction recreate evidence from the 2026.18.0 battery-gain
validation says continuous presence is what matters).

Nothing could say it, though. The reconcile planner is keyed on `publishedAs`,
so a desired spec carrying a live identity with a new `indigoDeviceId` and the
same role planned as an ordinary `update` — and `Registry.update()` resolves
the live set by the NEW device id, finds nothing, and aborts the whole
reconcile with `unknown_device`. The declarative truth "device B now drives
identity X" was unrepresentable. How should it be represented, and through
which affordance is a user allowed to cause it?

## Decision Drivers

* **ADR-0010's ownership split must hold.** The plugin decides which device
  drives which identity; the node allocates and persists numbers. A migrate
  must not add a node-side identity decision.
* **`attach` is a full reconcile, not a delta.** Whatever expresses a migrate
  must survive the very next attach — anything the store does not persist and
  the reconcile does not honour is undone at the next reconnect.
* **Zero wire activity is the entire point.** The measured Apple behaviour
  makes any removal window — however brief — a room/name purge. A migrate that
  closes and recreates the endpoint is just the orphan flow with better
  marketing.
* **The `upsert_endpoint` guard (issues #219/#240) must not be relaxed.** It
  refuses a *device* silently changing its *identity*; issue #246 asked for a
  sanctioned way through the gate, not a hole in it.

## Considered Options

* A `rekey` outcome in the attach reconcile, driven declaratively by the store
  (chosen)
* A dedicated `migrate_endpoint` command
* Relax the `upsert_endpoint` guard to accept the move

## Decision Outcome

Chosen option: **a `rekey` outcome in the attach reconcile**. The plugin's
"Migrate an exported accessory…" menu action retargets the `ExportStore`
entry — the source entry leaves the list, the target entry claims the
identity, in one atomic `replace_all` commit — and sends one mid-session
`attach`. The planner, seeing a live identity whose desired spec carries the
same role but a different `indigoDeviceId`, plans a **rekey**: the registry
moves the live entry to the new device id, re-wires command routing to it,
and applies the new label and states. Nothing is closed, nothing is created,
the number is untouched, `ConfigurationVersion` is not bumped, `PartsList`
does not change — no ecosystem processes a removal or an addition, which is
the invariant the room/name/scene survival rests on. (The label/reachable/
state application is an ordinary attribute update, and subscribable: a state
mismatch between old and new device is visible as a state change — harmless
in the same-logical-device flow this exists for.) A retarget combined with a
role change or a
battery gain is NOT a rekey and falls back to the existing recreate path;
the plugin refuses cross-role migrate at the dialog anyway (#219 E3, #240).

The `upsert_endpoint` gate gains a door-sign rather than a hole: a spec whose
identity is live on a *different* device is now refused explicitly (naming the
attach reconcile as the sanctioned path) instead of falling into `create()`
and dying on a duplicate `Endpoint.id` inside matter.js.

### Consequences

* Good, because the migrate needs no new protocol surface at all — no command,
  no result shape, no event, no frames.json change, protocolVersion stays 2.
  Version skew degrades loudly, not silently: an old node given a rekey attach
  aborts its reconcile with `unknown_device` rather than duplicating anything.
  The abort is not side-effect-free — an old node applies the plan's removals
  and creates before the rekey-shaped `update` aborts it, so a ●-target
  migrate against a skewed node removes the target's old accessory and then
  fails loudly — but the pinned install spec and version-together rule make
  the skew window an install-time concern, not a runtime one.
* Good, because restart-survival is free: the store is the persistent truth
  and every future attach re-asserts the same rekey-free steady state.
* Good, because re-adopt's honest story gets simpler to tell — the orphan flow
  is now *only* the recovery fallback, and its dialog stops overclaiming what
  Apple preserves (corrected in the same change, per the measured facts).
* Bad, because a rekey is invisible by design, so a mis-migrate (wrong target
  picked) shows no ecosystem symptom at all — the confirmation WARNING in the
  Indigo log is the only witness. Accepted: the dialog's re-verification
  ladder and the ● replacement warning are the mitigation.
* Bad, because the reconcile plan grows a fourth bucket that every future
  planner change must reason about. Accepted: the alternative was a fourth
  *command*, which is strictly more.

### Confirmation

`bridge-node/test/reconcile.test.ts` (rekey planning, recreate fallback on
role/battery change, mass-removal guard indifference),
`registry.test.ts` (number stability, command re-routing, no
ConfigurationVersion bump, the claimed-identity upsert refusal),
`tests/test_export_menu.py` (the migrate dialog's full validation ladder, the
atomic store commit, the corrected re-adopt wording),
`tests/test_export_bridge.py` (the mid-session reattach nudge). `npm test`
and `pytest` both green.

## Pros and Cons of the Options

### A `rekey` outcome in the attach reconcile (chosen)

* Good, because it is ADR-0010's own mechanism finishing the job: one field,
  now movable in the one remaining direction, still declared by the plugin
  and still reconciled by the node.
* Good, because the interactive path and the restart path are the same code —
  there is no "migrate worked but the next reboot undid it" class of bug.
* Neutral, because a full attach is a heavier nudge than a single-endpoint
  command — but attach cost scales with export count, which is small by
  construction (the node warns past 50).

### A dedicated `migrate_endpoint` command

* Good, because it reads as the most direct statement of intent, and the
  issue's own implementation notes guessed this shape.
* Bad, because the reconcile rekey is needed *anyway* — without it the first
  post-migrate attach aborts — so the command is strictly additional surface
  (protocol doc §, client method, frames, skew story) buying only an
  immediacy the attach path already provides. ADR-0010 rejected
  `readopt_endpoint` on the same grounds.

### Relax the `upsert_endpoint` guard

* Good, because no new machinery at all.
* Bad, because the guard exists to make accidental identity moves structurally
  hard (#219 E12); an upsert that sometimes moves identities is exactly the
  ambiguity it was added to kill. The gate stays; it just names the door.

## More Information

Design: `.scratch-246-design.md` (workspace-local, not committed), owner
rulings 1-3. Measured Apple purge behaviour: issue #246, 2026-08-18 jarvis
validation of PR #244. Mechanism this builds on:
[ADR-0010](0010-a-published-accessory-identity-is-plugin-owned.md). Protocol:
[`../BRIDGE_PROTOCOL.md`](../BRIDGE_PROTOCOL.md) §3.1 (rekey outcome), §3.2
(claimed-identity refusal). Issue: #246.

## For AI agents
- DO: treat "same `publishedAs`, same role, new `indigoDeviceId`" as a rekey —
  an in-place, zero-wire retarget. Never plan or implement it as remove+create;
  the removal window is precisely what purges the user's room and name.
- DO: keep the store commit atomic (`replace_all`) when moving an identity
  between entries — a transient state with two entries claiming one identity
  refuses the entire next attach.
- DON'T: relax the `upsert_endpoint` claimed-identity/role guards to "support
  migrate" — migrate goes through attach, deliberately.
- DON'T: offer cross-role migrate. A role change retires the identity
  (ADR-0010 generation bump); pretending otherwise re-opens #240.
