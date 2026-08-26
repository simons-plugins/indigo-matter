---
parent: Decisions
nav_order: 16
title: "ADR-0016: Endpoint removal is unconditionally destructive — there is no soft removal to opt out of"

status: "accepted"
date: 2026-08-26
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0016: Endpoint removal is unconditionally destructive — there is no soft removal to opt out of

## Context and Problem Statement

ADR-0015 (accepted one day earlier, 2026-08-26) established the decision this
one does not revisit: a **confirmed** device deletion destroys the accessory,
with no orphan retention and no re-adopt. That ruling stands in full.

What ADR-0015 also recorded, and what this supersedes, is the **mechanism** it
was reached by. It shipped in two halves. The first added destruction as an
opt-in — the node went on orphaning by default and a new `permanent: true`
wire flag asked it not to — and ADR-0015 was written against that half, while
the second half, deleting the re-adopt UI, was still pending.

Two of its consequences are therefore now false:

> * Good, because the destructive path is opt-in per call (`permanent`, …)

> * …supersede/re-adopt sequence, are structurally unable to trigger it.

Once the re-adopt UI was deleted, nothing in the plugin ever wanted an orphan.
A default that no caller selects is not a safety property; it is an unused
branch, a second wire vocabulary, and a second set of node-side bookkeeping to
keep correct. The question this ADR answers: **once nothing retains an orphan,
should the opt-out remain?**

## Decision Drivers

* An opt-in whose every caller opts in is dead weight that reads like a choice.
* A wire field with one lawful value is a compatibility liability, not a hedge.
* The `orphaned`/`orphanedAt` markers only had meaning for the re-adopt picker.
* Whatever guards a wrongly-issued removal must guard it in one place, not two.

## Considered Options

* **Remove the flag; every removal destroys** (chosen)
* Keep `permanent` as a documented no-op default-true, for wire compatibility
* Keep the soft path for `replace()`'s two-command supersede sequence

## Decision Outcome

Chosen: **remove the flag; every removal destroys.** `remove_endpoint` has one
meaning again — the endpoint is gone, and its `endpoint-map.json` record with
it. `permanent`/`ARG_PERMANENT`, `parsePermanentRemoval`, `list_orphans`,
`OrphanRecord`, `EndpointMapStore.orphans()`, `noteReadoptableMatch()` and the
`orphanedAt` field are deleted on both sides of the wire.

We own both ends of this protocol and `protocolVersion` is what protects us
(`bridge_protocol.py` is explicitly **not** a rename firewall), so a field with
one lawful value earns nothing by surviving.

### Consequences

* Good, because there is one removal path to reason about rather than two, and
  no default anybody has to know is safe.
* Good, because `endpoint-map.json` now records only live endpoints. It was
  already the independent witness rather than the allocator; it is now a
  witness to a smaller, truer thing.
* Bad, because `replace()`'s literal two-command wire sequence now destroys and
  re-adds rather than superseding in place. **Verified not to break migrate:**
  accessory numbers come from matter.js's own persisted allocation, not from
  this witness file, so the number and the identity move survive. What is lost
  is only the retroactive `supersededBy` marking for that literal sequence; it
  remains for the reconcile-based case. Three bridge-node tests that drove the
  old sequence and asserted the soft bookkeeping were rewritten to assert the
  destroy-then-fresh-add reality.
* Bad, because destruction is now unconditional, which makes
  `mass_removal_refused` (`reconcile.ts`) the single remaining guard between a
  plugin bug that sends a short list and a wiped fabric. It is untouched and
  deliberately so; it should be the first thing anyone re-reads before changing
  how the export list is computed.
* Neutral: `supersededBy`, `orphaned` and `deviceId` survive on
  `endpoint-map.json` records. They read as re-adopt leftovers and are not —
  they serve `restorable()`'s startup rebuild (issue #141) and migrate's
  driving-device rekey detection in `check()`. Do not delete them on the
  strength of the name.

### Confirmation

`grep -rn "orphan"` over the plugin and the bridge node returns only historical
citations and the unrelated launchd orphan-process reaping in
`launch_agent.py`/`server_process.py` — a different word for a different thing.

Suites after the deletion: **3804** Python (from 3891) on 3.11 and 3.13, and
**673** bridge-node (from 697). Both fell because a feature and its tests were
removed, which is the intended direction; migrate's own 56 tests still pass.
Net diff **+700/-3355** across 38 files.

## More Information

Supersedes the mechanism recorded in
[ADR-0015](0015-a-confirmed-deletion-destroys-the-accessory-no-retention.md);
its ruling — that a confirmed deletion destroys — is unchanged and remains the
governing decision. ADR-0015 is not edited, per this repo's immutability rule.

The staging that produced the stale text was a sequencing error worth naming:
the ADR was written against a half-finished migration whose second half was
already planned and already known to remove the flag. An ADR should be written
against the end state, or explicitly marked as describing an interim one.
