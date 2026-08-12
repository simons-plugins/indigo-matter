---
parent: Decisions
nav_order: 9
title: "ADR-0009: Indigo roots a device group by age, so membership is what this plugin delivers"

status: "accepted"
date: 2026-08-12
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0009: Indigo roots a device group by age, so membership is what this plugin delivers

## Context and Problem Statement

[ADR-0008](0008-a-matter-node-is-an-indigo-device.md) decided that a node's
synthetic `matterNode` device would be **the root** of its endpoint devices'
Indigo device group (option B), and the grouping follow-up was built on that
promise. On the live rig it did not hold: every join warned, nodes came out as
one two-member pair instead of one group of N+1, and each "fix" (reverse the
argument order; ungroup the stale pair first; pre-heal once per node before
the sweep) made the warnings move rather than stop.

The Indigo docs do not say which member of a group becomes its root. Two
readings had already been inferred from field observation and both were
wrong. So the question was settled by a **controlled experiment** on the live
server (jarvis, via `indigo-host`, 2026-08-12) rather than by another
inference:

```
dissolve → node=[N] motion=[M] temp=[T]
groupWithDevice(motion, node) → [motion, node]     (child roots)
groupWithDevice(node, motion) → [motion, node]     (IDENTICAL — arg order IRRELEVANT)
then groupWithDevice(temp, node) → [motion, temp, node]  (accumulates; root unchanged)
```

Reading it plainly: **Indigo orders a group's members by device AGE (creation
order), and the OLDEST member is the root** — `getGroupList`'s first element,
identical whichever member is asked. `groupWithDevice`'s argument order does
**nothing**. Adding a member to an existing group accumulates and does not
move the root.

Three consequences follow immediately, and they are the whole problem:

* A synthetic node device is, by construction, **younger** than every endpoint
  device on any node commissioned before it existed. It can therefore *never*
  root a fielded install's group. No argument order, no ungroup-and-rejoin, no
  sweep ordering changes that.
* It can root a **fresh** commission's group, and only by being created
  **first** — creation order is the sole lever that exists.
* The "misroot" warnings were Indigo working exactly as designed, reported to
  the user as a fault. A partial group is not damaged either: it heals by
  simply adding the remaining members (the experiment's third line).

## Decision Drivers

* **The experiment is the authority.** ADR-0003's discipline — evidence from
  the device itself, never a human transcription or an inferred rule — applies
  to Indigo's own undocumented behaviour just as much as to a Matter node's
  AttributeList. Two prior readings of `groupWithDevice` were inferred from
  incidental observation and both were wrong.
* **Never warn a user about something they cannot act on, and never about
  something that is not wrong.** A warning per grouped device per reconnect,
  about Indigo's own ordering rule, is noise that trains users to ignore the
  event log.
* **Fielded installs must not be treated as broken.** Whatever this decision
  is, it has to leave a 2026.13.x install in a good state without asking the
  user to delete and recommission anything.
* **What the user actually gets from grouping is that a node's devices show
  together.** That is visible in the Indigo device list and is delivered by
  membership alone. Rooting is a secondary property that decides where
  `batteryLevel` is read from.

## Considered Options

* Membership over root — group everything, accept Indigo's age-based root, and
  make the node device root a group only where creation order allows it
  (chosen)
* Group fresh commissions only — group nothing on a fielded install, since the
  node device cannot root it there
* Drop grouping entirely — keep the node device, ungrouped, and revert to
  ADR-0008 option A

## Decision Outcome

Chosen option: **membership over root**. One group per node, containing every
one of that node's Indigo devices including the node device. Indigo's
age-based root is accepted as given: on a **fresh** commission `create_devices`
creates the node device **before** the endpoint loop so that it is the oldest
member and therefore the root; on a **fielded** install the oldest endpoint
device leads and that is fine, stated plainly in the user docs rather than
warned about. The misroot warning class is retired, along with the pair
self-heal and the per-node pre-heal that existed to serve it.

### Correction to ADR-0008 — supersedes in part

ADR-0008's decision that a Matter node is an Indigo device, and everything it
says about *why* (the cross-controller survey, the ADR-0003 creation gate, the
rejection of a DeviceFactory and of a collapsed HA-shaped device) **stands
unchanged**. What this ADR supersedes is one claim inside option B: that the
node device is *the root* of its endpoint devices' group. It is the root only
where it is the oldest member. Membership in one group, plus rooting on fresh
commissions, replaces "always the root".

ADR-0008's "For AI agents" note also says to create the node device **after**
the per-endpoint creation loop, partly so that an endpoint device wins the
bare-name collision. That ordering is reversed here for the reason above. The
half of the note that still holds absolutely is the other half: the node
device's spec must **never** be appended to `plan`, because `multi` and
`role_counts` must see endpoint specs only.

ADRs are immutable, so this is recorded here and ADR-0008 is not edited.

### Consequences

* Good, because the node's devices genuinely end up in one group, which is the
  part a user sees, on fresh and fielded installs alike.
* Good, because a whole class of warnings disappears — they were reporting
  Indigo's designed behaviour as a fault, once per grouped device per
  reconnect.
* Good, because the grouping sweep becomes order-independent: a partially
  grouped node heals by accumulation, so no pre-heal, no self-heal, and no
  reasoning about which sibling the sweep reaches first.
* Bad, because the root convention — Indigo reads `batteryLevel` from the
  device leading a group — only points at the node device on fresh
  commissions. On a fielded install it points at the oldest endpoint device,
  which carries `batteryLevel` itself whenever issue #205's PowerSource
  coverage includes it, so the convention still lands on a battery-bearing
  device; and the node device carries the node-wide reading regardless of what
  roots anything.
* Bad, because `delete_node` can no longer rely on deletion ORDER:
  `indigo.device.delete` refuses to delete a non-empty group's root, and on a
  fielded install that root is an endpoint device, so children-before-node-
  device would throw on the first child. It now **ungroups every candidate
  first and then deletes**, which is indifferent to which end Indigo picked.
* Bad, because creating the node device before the endpoint loop costs a
  re-run of its name true-up after the loop: mid-migration an endpoint device
  may still be holding the bare base when `_unique_name` looks, so the node
  device can be born `"<base> 2"`. The post-loop true-up (the same idempotent
  one that heals a 2026.13.0 install) renames it in the same pass.

### Confirmation

The Python suite: `tests/test_device_sync.py`. Its `FakeDeviceFactory` now
models the experiment's semantics directly — age-ordered membership, a
symmetric `groupWithDevice`, and a `delete` that refuses whichever device
roots a non-empty group — and one test reproduces the three-line transcript
above against it, so a future change that reintroduces argument-order
dependence fails there first. Membership assertions replace root assertions on
every fielded-install test; the fresh-commission tests assert the node device
roots the group *and* that it was created first, so moving the
`_ensure_node_device` call back after the endpoint loop fails a test rather
than silently demoting the node device. `python3 -m pytest -q` green.

Live confirmation is the experiment itself, which was run on the fabric this
plugin drives.

## Pros and Cons of the Options

### Membership over root (chosen)

* Good, because it delivers the visible benefit — one group per node — on
  every install, new or fielded.
* Good, because it stops fighting a rule that cannot be fought, and deletes
  the code that was fighting it.
* Neutral, because rooting becomes a property of *when* a node was
  commissioned rather than a guarantee. Stated in `docs/MATTER.md` in those
  terms.
* Bad, because two installs of the same hardware can have differently-led
  groups, which is one more thing to explain.

### Group fresh commissions only

* Good, because every group the plugin makes would then be rooted at its node
  device, keeping ADR-0008's promise literally true.
* Bad, because it abandons every fielded install to no grouping at all — the
  users with the most devices get the least benefit, to preserve a property
  they cannot observe.
* Bad, because "grouped or not, depending on when you bought it" is a worse
  thing to explain than "led by your oldest device".

### Drop grouping entirely

* Good, because it is the smallest possible amount of code.
* Bad, because it throws away the one part of ADR-0008 option B that works
  everywhere and that users can see, on account of a secondary property.
* Bad, because it would leave the node device as ADR-0008's own option A
  describes it: "a smaller, less useful version of what every other
  controller in the cross-controller survey actually does".

## More Information

Supersedes in part [ADR-0008](0008-a-matter-node-is-an-indigo-device.md) (the
root claim inside option B, and its creation-order note); ADR-0008 otherwise
stands. Related: [ADR-0003](0003-attributelist-is-the-capability-authority.md)
(evidence over inference, the discipline this experiment applies to Indigo
itself). Module-level description: [`../ARCHITECTURE.md`](../ARCHITECTURE.md)'s
`device_sync.py` row. User-facing wording: [`../MATTER.md`](../MATTER.md), "One
extra device per node".

Issues: #204 (the node device and this grouping stage), #205 (the PowerSource
coverage the battery caveat above depends on).

## For AI agents
- DON'T: fight Indigo's age-based root. `groupWithDevice`'s argument order does
  nothing, ungrouping-and-rejoining changes nothing, and sweep order changes
  nothing — the oldest member roots the group, full stop.
- DO: create the node device BEFORE the endpoint creation loop in
  `create_devices`, so it is the oldest member on a fresh commission — and
  still never append its spec to `plan` (`multi`/`role_counts` must see
  endpoint specs only).
- DO: verify MEMBERSHIP (`node_dev_id in getGroupList(dev_id)`) after a
  grouping call, never position, and dissolve a node's group before deleting
  any of its devices.
- DON'T: reintroduce root-order warnings, a pair self-heal, or a pre-heal.
  They reported Indigo's designed behaviour as a fault, and a partial group
  heals by accumulation on its own.
