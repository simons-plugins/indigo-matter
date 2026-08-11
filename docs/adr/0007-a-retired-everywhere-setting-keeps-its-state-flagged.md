---
parent: Decisions
nav_order: 7
title: "ADR-0007: A setting retired everywhere keeps its state, flagged — only a missing capability withdraws it"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0007: A setting retired everywhere keeps its state, flagged — only a missing capability withdraws it

## Context and Problem Statement

[ADR-0003](0003-attributelist-is-the-capability-authority.md) settled how a
device **state** is withdrawn per unit: only a positive NO from that unit's own
AttributeList removes it (issue #190), because a state a trigger can bind to
must never vanish out from under one. [ADR-0006](0006-a-curated-exclusion-list-cross-checked-by-the-generator.md)
then retired the `onTime` **setting** everywhere — the ConfigUI field, the
`DeviceSetting`, and the `<State id="onTime">` all went, on every `matterRelay`,
regardless of what any individual unit's AttributeList said.

That third removal is not the ADR-0003 case. ADR-0003 withdraws a state
**per device**, on **positive evidence a specific unit lacks the attribute**.
Retiring `onTime` withdrew a state **for every device of the type**, on
**no such evidence at all** — the attribute is real and several fielded units
do implement it; the setting built on it just never worked. ADR-0003's
Decision Drivers asserted, without a citation, that "withdrawing a device state
breaks any trigger or control page bound to it, silently." A live experiment
against the GRILLPLATS (2026-08-11, recorded in ADR-0006's Confirmation
section) tested that claim directly by removing a state a trigger was bound to,
and confirmed it — worse than confirmed: the trigger stayed `enabled: true`,
Indigo logged nothing, and the trigger's own dialog silently repointed at a
different, real state in place of the one that vanished. A user who opens such
a trigger only to look, and saves, rebinds it to whatever the dialog now shows.

So the `onTime` retirement paid exactly the cost ADR-0003 exists to avoid,
for a reason ADR-0003 was never written to cover: not "this unit doesn't have
it" but "this plugin has decided the setting is inert everywhere." The question
this ADR answers: when a setting is retired for that second reason, does the
state have to go too?

## Decision Drivers

* The live experiment is now first-hand evidence, not an assumption ADR-0003
  carried on its own authority — and what it shows is worse than "stops
  firing," which raises the cost of ever withdrawing a state a real user could
  be bound to.
* The Indigo SDK offers an option neither ADR-0003 nor ADR-0006 used:
  `updateStateOnServer`'s optional `uiValue` parameter sets a UI-only display
  string that is **"not used in triggers or conditional logic"** (Indigo SDK
  reference, API v1.6+). A phantom `0` and a flagged `<unused>` are not the
  same failure mode — `uiValue` is exactly the tool that makes them
  distinguishable.
* ADR-0003's own asymmetry — states are hard to remove safely, ConfigUI
  fields are not — already implies withdrawal should be reserved for when it
  is the only option. A retired-everywhere setting always has another option:
  there is no per-unit capability question left open, so there is nothing a
  kept-but-flagged state could wrongly claim a device supports.
* `onTime` shipped in `v2026.10.1`. Any device that had the state carries a
  trigger surface an owner may already have used, no matter how briefly the
  setting existed for.

## Considered Options

* Keep treating "capability the unit lacks" and "setting retired everywhere"
  as the same case — withdraw the state either way (status quo, ADR-0006's
  own `onTime` removal)
* Keep the state for a retired-everywhere setting, but leave it at Indigo's
  default value with no marking
* Keep the state, and flag it distinguishably with `uiValue`

## Decision Outcome

Chosen option: **keep the state and flag it with `uiValue`**, for the
retired-everywhere case specifically. The capability case is untouched:

* **A capability the unit lacks (#190)** → *withdraw* the state, per unit,
  exactly as ADR-0003 already governs. A state that is never offered cannot
  have a doomed trigger built on it, because the device genuinely does not do
  the thing.
* **A setting the plugin is retiring everywhere (#197)** → *keep the state and
  flag it with `uiValue`*. There is nothing left to prevent — no unit
  implements the setting in a way that matters — so the only thing withdrawal
  could do here is break bindings that already exist.

`matterRelay`'s `onTime` state is restored to `Devices.xml`, unconditionally
for the type (not gated by `unimplemented_states` — no `DeviceSetting` backs
it any more, so there is nothing left to gate on). `plugin.deviceStartComm`
calls `_prime_retired_on_time_state` on every start, which sets it to `0` with
`uiValue="<unused>"` via the existing `device_sync.apply_states` seam. Nothing
else writes it: `on_off.py`'s `SETTING_STATES` no longer maps `OnTime`
(ADR-0006), so the flagged value is never overwritten by a real attribute
update.

This narrows, but does not reopen, ADR-0006's actual finding: `onTime` was
correctly identified as **not a setting** — the spec review stands. What
changes is only what happens to the **state** that setting left behind.

### Consequences

* Good, because a trigger built on `onTime` before it was retired stays bound
  to what its author actually built, rather than silently repointing at a
  different state the next time someone opens it.
* Good, because `_announce_retired_on_time_state`'s notice ([`plugin.py`](../../indigo-matter.indigoPlugin/Contents/Server%20Plugin/plugin.py),
  #197) no longer has to warn about a broken binding that no longer exists —
  it only has to say the setting is gone from the dialog and where the state
  now stands.
* Good, because `uiValue` is display-only and API v1.6+ stable — this costs
  nothing at runtime beyond the one extra key in an existing
  `updateStatesOnServer` call, and it cannot be read by a trigger or
  conditional, so it cannot reintroduce the #190 "phantom zero" failure.
* Bad, because the rule now has two branches instead of one, and a future
  retirement has to be classified correctly before anyone can apply either
  half — get the classification wrong and either a live capability is
  withdrawn needlessly or a genuinely dead state is left looking real.
* Bad, because a flagged state is still a line in `dev.states` forever — it is
  not free, only cheaper than the alternative this ADR rejects.

### Confirmation

`tests/test_plugin_module.py::test_a_flagged_retired_state_is_present_not_withdrawn`
asserts `onTime` is present on `matterRelay` and absent everywhere else — the
inverse of what the pre-ADR-0007 version of that test asserted.
`test_no_retired_setting_leaves_a_configui_field_behind` keeps the ConfigUI
half of the old combined test unchanged: the field is still gone, on every
type, no exceptions.
`test_device_start_comm_primes_the_retired_on_time_state_for_relays`,
`test_device_start_comm_does_not_prime_on_time_for_other_types`,
`test_prime_retired_on_time_state_writes_zero_and_unused_uivalue`, and
`test_prime_retired_on_time_state_never_raises` cover the priming seam itself,
including that it can never block device start.
`test_the_notice_says_the_state_stays_bound_and_flagged_unused` pins the
softened retirement notice and asserts none of "error"/"fail"/"warning"/
"problem" appear in it.

The live experiment itself — method, control, and the raw observations — is
recorded in [ADR-0006](0006-a-curated-exclusion-list-cross-checked-by-the-generator.md)'s
Confirmation section rather than repeated here, because ADR-0006 is where it
was actually run.

## Pros and Cons of the Options

### Withdraw the state either way (status quo)

* Good, because it needed no further work — it is what ADR-0006 already did.
* Bad, because it is now proven, not assumed, to silently orphan any trigger
  a user built before the retirement, and to do so in the worst available
  way: enabled, unlogged, and quietly repointed at a different real state.

### Keep the state at its default, unmarked

* Good, because it needs no new code — Indigo already initialises an
  undeclared-then-redeclared state to `0`.
* Bad, because a bare `0` is exactly the #190 failure ADR-0003 exists to
  prevent: indistinguishable from a real reading, and `0` is not even a
  plausible-looking value for `onTime` here since it was the value this
  setting always held anyway.

### Keep the state, flagged with `uiValue`

* Good, because it is the only option that both preserves the binding and
  makes the state's deadness visible without opening a trigger.
* Bad, because it is one more runtime write, on every device start, for a
  state that will never again carry real information.

## More Information

Does **not** supersede [ADR-0003](0003-attributelist-is-the-capability-authority.md) —
its per-unit, positive-NO withdrawal rule is exactly right for the capability
case (#190) and continues to govern it unchanged. This ADR narrows *when*
withdrawal applies: only to "this unit doesn't have it," never to "this
plugin decided the setting is inert." It also does not revisit
[ADR-0006](0006-a-curated-exclusion-list-cross-checked-by-the-generator.md)'s
finding that `onTime` is not a setting — only what happens to the state that
finding leaves behind.

`uiValue` reference: Indigo SDK, `Device.updateStateOnServer` — *"the optional
`uiValue` parameter is used to set UI only display string of the value which
is not used in triggers or conditional logic (API v1.6+)."*

Module-level description: [`../ARCHITECTURE.md`](../ARCHITECTURE.md) rows for
`plugin.py` (`deviceStartComm`, `_announce_retired_on_time_state`) and
`device_sync.py` (`apply_states`).

Issues: #190 (per-unit capability withdrawal), #186 (shipped the `onTime`
setting), #197 (retired it; this ADR is its live-experiment follow-up).

### Addendum (2026-08-11): a label names the attribute, not the behaviour

`onTime`'s `TriggerLabel`/`ControlPageLabel` shipped in #186 as `Auto-Off
Timer (retired — do not use)`. That label was wrong at the root, and not
because it needed a "(retired)" suffix — it never named the thing it was
labelling. `OnTime` (0x4001) is a parameter of a command this plugin never
sends; there was never an auto-off timer, so "Auto-Off Timer" was not a stale
name for the state, it was a claim about a behaviour the attribute never had.
That claim outlived the feature it described by a full release, because a
label is read far less often than the code around it is changed — the same
failure mode this repo already knows from comments, on a surface (Indigo's
trigger and control-page editors) a comment audit never reaches.

The generalisable rule this leaves behind: a state's `TriggerLabel`/
`ControlPageLabel` must name the Matter **attribute** it carries, never
assert the **behaviour** the author hoped it would have. A label that makes
a claim ages exactly as badly as a comment that makes one — worse, because a
user reads it with no source history to check it against. `onTime`'s label
is now `OnTime (retired)`: the honest attribute name, plus the one fact that
is still true. "Auto-Off Timer" is gone from `Devices.xml`; the retirement
notice in `plugin.py` (`_announce_retired_on_time_state`) now names the
state on both surfaces a user might be looking at — the device inspector's
KEY (`onTime`) and the trigger editor's LABEL (`OnTime (retired)`) — instead
of repeating the retired label.

## For AI agents
- DO: withdraw a state per unit only on positive evidence that specific unit
  lacks the capability (ADR-0003) — never for a setting the plugin is retiring
  across the board.
- DO: for a setting retired everywhere, keep its `<State>` in `Devices.xml`
  and flag it with `uiValue` (a display-only string, API v1.6+) rather than
  removing the declaration.
- DO: cite the live experiment in ADR-0006's Confirmation section, not
  ADR-0003's original (uncited) Decision Drivers claim, when justifying why a
  state must not be withdrawn out from under an existing trigger.
- DON'T: treat "the setting doesn't work" and "this unit doesn't have the
  attribute" as the same reason to remove a state — only the second is.
- DON'T: leave a flagged-but-kept state at a bare numeric default. Set a
  `uiValue` that reads as dead, not as a plausible reading.
- DON'T: read this ADR as reopening whether `onTime` is a setting — that
  question is ADR-0006's, and it stands.
