---
parent: Decisions
nav_order: 12
title: "ADR-0012: For a device Indigo does not type, export eligibility is a user declaration"

status: "accepted"
date: 2026-08-18
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0012: For a device Indigo does not type, export eligibility is a user declaration

## Context and Problem Statement

`export_catalog.classify` answers "may this device be exported, and as what?"
from the device alone: its `pluginId` for the loop guard, its IOM class for the
role rules, its capability flags for the details. That has been true since E2
and it is what makes the function safe to re-run as the guard at endpoint-build
time, in the startup reconcile and on every dialog interaction.

It also has a blind spot, measured on the reference database (jarvis,
2026-08-18, 678 devices):

| verdict | devices |
| --- | --- |
| `no resolvable Matter role` | **326** |
| eligible (all roles combined) | 313 |
| `sensor reports neither an on/off state nor a value` | 3 |

The largest single group of exclusions was the largest group in the database,
and the reason it gave was misleading. A plugin declaring
`<Device type="custom">` gets a plain `indigo.Device` — verified with
`indigo-host` against a live Texecom zone, whose MRO is
`Device, BaseElem, instance, object`. It matches no entry in the class table,
so it never reaches a role rule at all, and `supportsOnState` is False because
the base class has no on/off concept. Its reading lives in a plugin-named
state.

Those 326 are not exotic. 229 of them publish a boolean state a user could
name as the device's reading — re-measured with the shipped classifier, which
is the figure below, not the raw boolean count. The
24 Texecom `alarmZone` devices are ordinary PIRs, door contacts, smoke and CO
zones — devices Matter models perfectly well, all reporting through a custom
`status` boolean, all told they had "no resolvable Matter role".

The problem: Indigo cannot tell us which state is the device's reading, and
nothing in the device says so. A plugin publishes `status`, `isApOrRouter`,
`lastTripped` and four `statusText.*` expansions with equal standing. Only the
user knows which one is the sensor — and for a fleet of identical zones, only
the user knows that this one is a PIR and that one is a smoke detector, because
the distinction exists solely in the device's name.

## Decision Drivers

* The guard must stay a guard. `classify` is re-run at endpoint-build time
  precisely so the allow-list cannot be the only thing standing between a
  hand-edited blob and the bridge.
* Wrong beats absent, but silent-wrong beats nothing. A fabricated `False` for
  a state that has gone would tell every ecosystem the smoke alarm is quiet.
* A role change now costs the accessory's room in every paired ecosystem
  (ADR-0010, issue #240), so the FIRST answer needs to be right.
* 24 identical zones must not mean asking the same question 24 times.

## Considered Options

* **A — leave them excluded.** Honest about the plugin's knowledge, useless to
  the user. Rejected: the devices are exportable, the plugin simply cannot
  work out how on its own.
* **B — guess the state.** Take the first boolean, or one matching a name
  list. Rejected as the primary mechanism: on a Texecom zone the naive guess
  picks between `status` and four `statusText.*` expansions, and a wrong guess
  is silent — an accessory that reports "tamper" as "occupied" looks like a
  working export. It survives only as the *pre-selection* inside option C.
* **C — the user declares the mapping, and the declaration is part of the
  eligibility question.** `classify` grows an `options` parameter; a custom
  device with a declared state key is eligible, one without is a new third
  verdict, `MappableDevice`.
* **D — synthesise a typed device.** Wrap the custom device in an adapter that
  fakes `onState` before classification. Rejected: it moves the same
  user-supplied fact somewhere the guard cannot see it, and every caller then
  has to remember to wrap.

## Decision Outcome

Chosen: **option C**. For a device Indigo does not type, export eligibility is
a function of the device *and the user's declaration about it* — carried in the
export entry's `options`, passed to `classify` by every caller that holds an
entry.

`classify` gains a third outcome. `MappableDevice` means "this could be
exported once you say which state is its reading", and it deliberately does
**not** carry `eligible_roles`: only `EligibleDevice` may answer that
attribute, so no caller can mistake an unmapped device for an exportable one by
duck-typing. Every guard therefore tests positively for `EligibleDevice`
rather than negatively for `Excluded`.

The mapping is two facts — which state, and whether it is inverted — stored as
`stateKey` / `stateInvert` in `options`, validated by `ExportEntry.from_dict`
against both shape and role, exactly as window-covering polarity already is.

### Consequences

* Good: 229 devices on the reference database become exportable, including
  all 24 Texecom zones — each offering exactly one candidate state (`status`),
  with the role resolving correctly from the name ("CO Sensor" → CO alarm,
  "Front Door" → contact, "Dining Room PIR" → occupancy). The largest
  exclusion bucket in the product turns into an answerable question. Already-
  eligible devices are unaffected: 313 before, 313 after.
* Good: eligibility is still checked at every guard. A mapped export whose
  state its plugin later renames re-classifies as `Excluded`, is skipped with
  a reason, and publishes nothing — rather than publishing a fabricated
  `False`.
* Good: the fleet answer is derived from the allow-list itself — a state key
  already chosen for another device of the same `(pluginId, deviceTypeId)`
  pre-selects for the next one. Nothing new is persisted, so nothing can go
  stale; the recipe IS the set of choices the user has made and kept.
* Bad: `classify(dev, plugin_id)` without options now under-reports a mapped
  device as merely mappable. Mitigated by the signature (options are the third
  parameter, defaulted) and by the callers being few and enumerated, but it is
  a genuine footgun and the reason the guards test positively.
* Bad: the role a mapped device takes is entirely the user's word. A zone named
  "Smoke Alarm TZ9" mapped as a contact sensor will export as a contact
  sensor. The name heuristic pre-selects; it does not police.
* Neutral: only booleans are mappable. A custom device with numeric states
  alone is told "not yet" rather than "never" — the same decision, applied to
  numeric sensor roles, is a separate one with its own evidence to gather.

## More Information

Issue #252 (umbrella), issue #236 (leak family), issue #179 (smoke/CO). The
population figures come from sweeping `export_catalog.classify` over the live
jarvis database on 2026-08-18; the device-class fact comes from `indigo-host`
against device 743389524. Related: ADR-0003 (a device's own evidence is the
capability authority) is narrowed here — it remains true for Matter devices
this plugin creates, where an `AttributeList` exists to read, and cannot apply
to an Indigo device whose plugin publishes no such thing.
