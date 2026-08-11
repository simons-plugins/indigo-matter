---
parent: Decisions
nav_order: 6
title: "ADR-0006: A curated exclusion list, cross-checked by the generator, decides what is not a setting"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0006: A curated exclusion list, cross-checked by the generator, decides what is not a setting

## Context and Problem Statement

[ADR-0005](0005-command-parameters-are-not-settings.md) chose the **command-field
rule** as the decision authority for which writable Matter attributes are not
settings: a writable attribute that is also named as a field of a command on its
own cluster is that command's parameter, generated into
`COMMAND_FIELD_ATTRIBUTES` and used to filter both the settable-attribute report
and (as a label) the explorer.

Issue #197's follow-up put that rule through a full spec review — Application
Clusters R1.5 and core sections, cited attribute by attribute — not because the
outcome it produced (retiring `onTime`) was wrong, but to check whether the
mechanism reached it for the right reason. It was not. The rule is right about
its four founding members for **three unrelated reasons**, is provably false as
a *general* premise, and a parser bug it depended on had been hiding a live false
positive that the fix cannot filter away. **ADR-0005 is therefore superseded.**
Retiring `onTime` and rejecting `quality: "N"` were the correct calls, and this
ADR restates why with the spec citations ADR-0005 didn't have.

## Decision Drivers

* The rule's own stated justification — "a command that takes the attribute as a
  parameter owns it" — is false for two of the four members it was praised for
  getting right. A rule that reaches the right answer for the wrong reason is
  lucky, not verified.
* The generator's `"RW" in access` test does not match matter.js's conditionally
  writable `R[W]` form, so it had been silently invisible to 13 attributes,
  including every `DoorLock` setting, since the day it shipped.
* Fixing that parser bug — which any future maintainer eventually will, because
  it is a plain string-match bug — demotes a real setting
  (`DoorLock.OperatingMode`) that cannot be excluded by narrowing the rule: its
  collision is with a *request* command's field, so excluding response commands
  does not help, and the four true command-field members share no `access` or
  `quality` signature that could be tested instead.
* ADR-0005 understated its own rejected alternative's damage: `quality: "N"`
  does not drop one real setting (`SmokeCoAlarm.SmokeSensitivityLevel`), it drops
  **46** writable attributes that lack it — more than twenty of them plainly
  user-facing — including an entire cluster
  (`ThermostatUserInterfaceConfiguration`) and attributes both Home Assistant and
  ZHA ship as user-facing config (`LevelControl.OnOffTransitionTime`/`OnLevel`/
  `OnTransitionTime`).
* A generated table will move every time the pinned `@matter/model` is repinned.
  Something has to guarantee a newly-introduced collision gets a human's
  judgement instead of silently falling on whichever side the mechanical rule
  happens to land.

## Considered Options

* Keep the command-field rule as the decision authority (status quo)
* Narrow the command-field rule to exclude request/response asymmetries
* Filter on write privilege (`Manage` or above vs below)
* A hand-curated `NOT_SETTINGS` exclusion list, each entry citing its spec
  section, with the generated `COMMAND_FIELD_ATTRIBUTES` demoted to a
  cross-checked **detector** rather than the filter itself

## Decision Outcome

Chosen option: **a hand-curated `NOT_SETTINGS` exclusion list, cross-checked by
the generator**.

### What survives from ADR-0005

Both of ADR-0005's real conclusions were right, and the spec review makes them
more certain, not less:

* **Retiring the `onTime` setting was correct — and is now confirmed by the
  specification itself, not by matter.js's implementation.** Application
  Clusters R1.5 §1.5.6.4: *"This attribute can be written at any time, but
  writing a value only has effect when in the Timed On state."* §1.5.7.6.4
  defines the decrement loop only inside `OnWithTimedOff`'s Effect on Receipt,
  and the §1.5.8 state machine (Figure 2) shows `OnWithTimedOff` is the **only**
  transition into Timed On. Independent corroboration: Home Assistant and ZHA
  expose no `OnTime` config entity; zigbee2mqtt implements `on_time` only as
  command 0x42, never as an attribute write.
* **Rejecting `quality: "N"` as the filter remains right — and ADR-0005
  understated it.** **46** writable attributes lack `N`, not one — more than
  twenty of them plainly user-facing:
  `LevelControl.OnOffTransitionTime`/`OnLevel`/`OnTransitionTime` (which Home
  Assistant and ZHA both ship as user config), the whole
  `ThermostatUserInterfaceConfiguration` cluster, several `DoorLock` settings,
  and `FanControl.RockSetting`/`WindSetting`.

### Why ADR-0005 is superseded — the rule was unsound

* **Its stated principle is false for half its own members.** "A command that
  takes the attribute as a parameter owns it, so a pre-set does nothing" holds
  for `OnOff.OnTime`/`OffWaitTime` (§1.5.6.4 above) but not for the other two of
  its four founding members. `Identify.IdentifyTime` — Application Clusters
  R1.5 §1.2.5.1: *"If this attribute is set to a value other than 0 then the
  device shall enter its identification state"* — writing IS the mechanism;
  there is no command that must run first. `GeneralCommissioning.Breadcrumb`
  — core §11.10.6.1 — is commissioner/administrator scratch space, written by
  design and zeroed on restart; nothing about it being a command's field
  parameter is why it fails to be a setting. Four right answers, reached
  through **three unrelated
  mechanisms** — a single mechanical test cannot be evidence for all three at
  once, however often it happened to land on the right side.
* **ADR-0005 claimed no false positive existed in the pinned model. That was an
  artefact of a parser bug.** The generator tested `"RW" in access` and so never
  recognised matter.js's `R[W]` (conditionally writable) form, hiding 13
  attributes from the rule entirely — twelve `DoorLock` settings including
  `AutoRelockTime`, plus `Thermostat.MinSetpointDeadBand`. With that fixed, the
  rule promptly demotes **`DoorLock.OperatingMode`** (§5.2.9.24,
  Normal/Vacation/Privacy/NoRemoteLockUnlock), a real, independently-readable
  user setting — because `SetHolidaySchedule` (§5.2.10.12.4) happens to have a
  request-command field of the same name. It cannot be narrowed away:
  `SetHolidaySchedule` is a request, not a response, so excluding `*Response`
  commands doesn't help, and the four true command-field members share no
  `access` or `quality` signature that a revised rule could test instead.

### The new decision

A hand-curated `NOT_SETTINGS` table in `settings_report.py`, each entry carrying
its spec citation, is now the filter the report and the explorer's labelling
obey. The generated `COMMAND_FIELD_ATTRIBUTES` is **demoted to a detector**: a
test asserts every attribute it derives appears in exactly one of `NOT_SETTINGS`
(confirmed not a setting) or `REVIEWED_COMMAND_FIELD_COLLISIONS` (confirmed a
real setting despite the name collision — currently `DoorLock.OperatingMode`).
A matter.js repin that introduces a new collision therefore fails CI and asks a
human to judge it, instead of the mechanical rule silently suppressing — or
silently keeping — a real setting.

### Also recorded

Findings from the same review that would otherwise be rediscovered the hard way:

* **connectedhomeip `master` diverges, and this makes the retirement stronger,
  not weaker.** An unreleased, code-driven OnOff cluster
  (`src/app/clusters/on-off-server/OnOffLightingCluster.cpp`, PR #42634) DOES
  start a countdown on a direct `OnTime` write, with a unit test
  (`TestWriteOnTimeUpdatesTimer`) asserting exactly that. It does not exist at
  tags v1.4.0.0, v1.4.2.0, or v1.5.0.0, so no shipping device behaves this way
  today — but it is spec-non-conformant against §1.5.6.4 regardless of whether
  it ships. A setting whose effect depends on which SDK snapshot the vendor
  happened to build against is worse than one that never works at all.
* **Certification does not settle it either.** `Test_TC_OO_2_1.yaml` writes
  `OnTime = 30` and asserts only that the read-back is 30 — "read-back proves
  nothing about meaning" is baked into the mandatory certification test plan
  itself. `Test_TC_OO_2_3.yaml` exercises Timed On exclusively via
  `OnWithTimedOff`, never via a direct write.
* **A fourth option was considered and rejected: filter on write privilege
  (Manage or above).** Core R1.5 §9.10.5.2 "AccessControlEntryPrivilegeEnum
  Type", table value 4 (Manage) — *"Operate privileges, and can modify
  persistent configuration of this Node"* — is the closest thing the spec has
  to a stated configuration marker (the per-value descriptions are unnumbered
  headings; in R1.2/R1.3 the same table is §9.10.4.2). Privilege ranks View 1,
  ProxyView 2, Operate 3, Manage 4, Administer 5, so the only reading under
  which the rule demotes what it should is "privilege is Manage or above" — a
  literal "privilege equals Manage" reading fails immediately, since it would
  also demote `Breadcrumb`. Read that way it still fails both ways:
  `Breadcrumb` is `RW VA` (Administer, one level above Manage) — it passes
  "Manage or above" and would be wrongly promoted to a setting — and
  `LevelControl.OnLevel`, `ThermostatUserInterfaceConfiguration.TemperatureDisplayMode`,
  and `BooleanStateConfiguration.CurrentSensitivityLevel` are all `VO`
  (Operate, below Manage) — they fail the filter and would be wrongly demoted
  from real settings.
* **Corrections to ADR-0005's own wording, recorded here because that ADR is
  immutable and cannot be edited.** "A pre-set value does nothing at all" and "a
  state that can only ever read 0" are overstated: a write *during* Timed On is
  normative and effective per §1.5.6.4, and any other admin on the fabric can put
  the device into that state at any time. "`OffWaitTime` is a guard preventing
  the device being turned back on" is imprecise: §1.5.6.5 and Figure 2 show it
  guards only against **another** `OnWithTimedOff` call; a plain `On` or
  `Toggle` transitions Delayed Off → On normally and zeroes it. "Matter has no
  auto-on" was verified for the OnOff cluster only (there is no `OffTime`
  attribute there) — it was never checked across all clusters and should not be
  read as a workspace-wide claim.
* **A count correction.** ADR-0005 said "four of 124 writable" and "30
  further attributes … excluded only by being read-only" — "110/" appears
  nowhere in ADR-0005 itself. After the parser and inheritance fixes the model
  has **141** writable attributes (not 124), and one member of that ~30 —
  `DoorLock.OperatingMode` — was never read-only at all; it was invisible to
  the buggy parser, which is a different failure than the one ADR-0005
  described.
* **A note on stale language elsewhere.** Commit `50136e3`'s message asserts the
  very inference this ADR rejects: "quality N … which is exactly what OnTime
  lacks and why OnTime is not a setting". `OnTime` is not a setting because the
  write is spec-inert (§1.5.6.4), full stop — `quality: "N"` was never the
  reason and is independently unsound as a rule (see Decision Drivers). Both
  [ADR-0002](0002-writable-settings-are-a-declarative-registry.md) and
  [ADR-0003](0003-attributelist-is-the-capability-authority.md) also carry a
  stale "57 writable attributes on handled clusters / would have excluded 55"
  figure — itself already wrong when written: four of the six attributes that
  take bounds from another device attribute were already present in that old
  57, not the two ADR-0002/0003 counted. Those ADRs are immutable, so the
  correction is recorded here instead: the true figures, as of this review,
  are **76** writable attributes on the clusters this plugin handles, of which
  **6** take bounds from another device attribute, so a registry requiring one
  would have excluded **70**. The argument those ADRs made survives unchanged
  — 6 of 76 is still a small minority — only the count was ever wrong.

### Consequences

* Good, because a matter.js repin that introduces a new command-field collision
  now fails a test and demands a human citation, rather than the rule silently
  deciding either way.
* Good, because every exclusion now carries its own spec citation instead of
  inheriting one derived rule's justification for all of them — the three
  unrelated mechanisms are visible in the data, not just in this document.
* Good, because `DoorLock.OperatingMode` is reported as the real gap it is,
  instead of being silently suppressed by a rule that was never evidence for it.
* Bad, because `NOT_SETTINGS` is hand-maintained and can, in principle, go stale
  the way the command-field rule did — mitigated by the same discipline this ADR
  applies to it: the detector, not discretion, decides when a new entry is
  needed.
* Bad, because the exclusion logic is now two data structures
  (`NOT_SETTINGS`, `REVIEWED_COMMAND_FIELD_COLLISIONS`) instead of one generated
  table, which is more to read before trusting the report. Accepted: the
  generated table alone was shown to be untrustworthy as a sole authority.

### Confirmation

`tests/test_settings_report.py::test_every_generated_command_field_collision_has_been_judged_by_a_human`
fails if `COMMAND_FIELD_ATTRIBUTES` ever contains a pair absent from both
`NOT_SETTINGS` and `REVIEWED_COMMAND_FIELD_COLLISIONS`.
`test_not_settings_and_reviewed_collisions_are_disjoint` fails if a pair is ever
filed as both a non-setting and a reviewed real setting.
`test_door_lock_operating_mode_IS_reported_as_a_gap` and
`test_the_explorer_does_not_label_door_lock_operating_mode_a_command_parameter`
cover the case this ADR exists for; `test_the_explorer_still_labels_on_time_a_command_parameter`
is the control case alongside it.

**Confirmed live on jarvis, 2026-08-11 19:16 UTC (20:16 BST), plugin
2026.12.0.** All seven commissioned nodes re-surveyed on restart — the banked
fingerprints are
`{"39":"1.0.26#[]", "46":"1.9.15#[]", "47":"1.0.21#[]", "49":"1.3.0#[]",
"50":"1.2.0-s2#[]", "52":"1.4.6#[]", "56":"1.1.3.8#[]"}` — every settable set
empty. So the report is silent because there is nothing to say, not because the
survey failed to run, and **ADR-0005's Confirmation claim still holds** after the
localization clusters were un-suppressed: no node on this fabric implements them.

Two claims this fabric cannot test, stated rather than assumed: there is no lock
commissioned, so `DoorLock.AutoRelockTime` appearing in the report is unverified
on hardware; and no node exposes a writable root-node attribute, so the
endpoint-0 wording is unverified on hardware. Both are covered by unit tests only.

**The retirement notice's per-device count was verified against real hardware and
is the reason the naive version was wrong.** The fabric has four `matterRelay`
devices; the notice reported **two**. What the hardware confirms is only this:
the two the notice reported (an IKEA ALPSTUGA and a Shelly 1PM Mini Gen3) were
the two whose persisted Indigo states still carried `onTime` when `startup()`
read them; the other two (the GRILLPLATS and the Tapo plug) did not. Counting
all four relays would have told two users their triggers may have broken when
those devices never had the state.

That result also settles, empirically, the ordering assumption the notice depends
on and which no Indigo document states outright: `startup()` runs before
`deviceStartComm()`, so `dev.states` still described the pre-upgrade world when it
was read. Had the states already migrated, the count would have been zero.

**A second live experiment, 2026-08-11 (times UTC; the server log is BST, one
hour ahead), settles a premise ADR-0003 asserted with no citation of its own —
"Withdrawing a device **state** breaks any trigger or control page bound to it,
silently" (ADR-0003, Decision Drivers) — and that ADR-0003's state-gate policy,
this ADR, ADR-0005, and `_announce_retired_on_time_state()`'s notice all inherit
unproven. ADR-0003 is immutable, so the confirming evidence is recorded here
instead, exactly as this ADR already does for ADR-0005.**

Method, compact enough to repeat: a throwaway `<State id="zzTriggerTest">` was
added to `matterRelay`'s `Devices.xml`, deployed, plugin restarted — it appeared
on the IKEA GRILLPLATS (device 1794293937) as `zzTriggerTest: 0`. A Device State
Changed trigger (`** MATTER TEST`, id 807063350) was bound to that state in the
Indigo UI and left enabled. The state was then deleted from `Devices.xml`,
redeployed, plugin restarted.

Observed after removal: the state vanished from the device's state dictionary
(`{…, onOffState, startUpOnOff, zzTriggerTest}` → `{…, onOffState,
startUpOnOff}`); the trigger still existed and still reported `enabled: true`;
Indigo logged nothing — no notice that a trigger references a state that no
longer exists; the trigger's own state dropdown, opened to inspect it, showed a
**different, real** state (the device's On/Off state) in place of the one that
had vanished; and toggling the plug off→on→off (`last_changed` confirms both
transitions) did not fire it.

**The control, without which the last observation is an assumption, not
evidence.** The trigger's action was a Domio push notification that turned out
to be independently misconfigured (`Domio Error: Notification body is
required`), so its silence during that first toggle proved nothing on its own —
a broken action and a trigger that never fired are indistinguishable from
outside. The trigger was then rebound to the live On/Off state and the plug
toggled again: it fired twice, and Indigo logged `Trigger ** MATTER TEST` on
each firing. **That log line is the actual evidence** — its total absence
during the state-deleted toggle is what shows the trigger did not fire, and its
presence here shows the trigger machinery and the device were both working
throughout, which is what makes the earlier silence mean something rather than
nothing. The first attempt at evidence (the push notification's absence) was
weak for exactly this reason and would have been worthless without the second.

This **confirms and strengthens** ADR-0003's premise; it does not contradict it.
It also sharpens the premise into something worse than "stops firing": a
trigger left bound to a withdrawn state does not error, does not disable
itself, and does not look unusual — it presents a plausible, real state in its
own dialog. A user who opens it, even only to inspect it with no intent to
change anything, and saves, rebinds it to whatever state the dialog now shows —
silently turning "a trigger that no longer fires" into "a trigger that fires on
something its author never wrote." `_announce_retired_on_time_state()`'s notice
(`plugin.py`, issue #197) now says so, rather than only naming the remedy.

## Pros and Cons of the Options

### Keep the command-field rule as the decision authority

* Good, because it needed no further work.
* Bad, because it is provably unsound as a general rule — right for its four
  founding members through three different mechanisms, not one.
* Bad, because the parser bug that hid its one false positive was always going
  to be fixed eventually, at which point the rule would silently start demoting
  a real setting.

### Narrow the command-field rule

* Good, because it would keep the "derived, not hand-maintained" property that
  made it attractive originally.
* Bad, because no narrowing exists: `SetHolidaySchedule` is a request command,
  so excluding `*Response` commands does not help, and the four true members
  share no `access` or `quality` signature a narrower rule could test instead.

### Filter on write privilege (`Manage` or above)

* Good, because Core R1.5 §9.10.5.2 gives `Manage` an actual spec definition —
  "Operate privileges, and can modify persistent configuration of this Node" —
  closer to a stated marker than anything else in the model.
* Bad, because reading the filter as "Manage or above" (the only reading under
  which it demotes what it should — a literal "equals Manage" reading would
  also demote `Breadcrumb`) still fails in both directions: `Breadcrumb`
  (`RW VA`, Administer — one level above Manage) would be wrongly promoted,
  and `LevelControl.OnLevel`,
  `ThermostatUserInterfaceConfiguration.TemperatureDisplayMode`, and
  `BooleanStateConfiguration.CurrentSensitivityLevel` (all `VO`, Operate —
  below Manage) would be wrongly demoted.

### A hand-curated `NOT_SETTINGS` list, cross-checked by the generator

* Good, because every exclusion carries its own citation instead of one rule's
  justification stretched over three unrelated reasons.
* Good, because the generated table still does real work — it is the thing that
  notices when the model moves, it just no longer decides alone.
* Bad, because a human must judge every new collision by hand; the detector
  guarantees this happens but does not do the judging.

## More Information

Supersedes [ADR-0005](0005-command-parameters-are-not-settings.md) in full;
ADR-0005 remains as the historical record of the command-field rule and is not
edited. Registry design is
[ADR-0002](0002-writable-settings-are-a-declarative-registry.md); capability
gating is [ADR-0003](0003-attributelist-is-the-capability-authority.md); the
read-only diagnostics rule is
[ADR-0004](0004-matter-diagnostics-are-read-only.md). The generated table
remains a build artefact because only the bridge node may import
`@matter/model` — that confinement is **workspace ADR-0006** (a different
sequence — see `docs/adr/INDEX.md`), not a decision of this ADR.

Module-level description: [`../ARCHITECTURE.md`](../ARCHITECTURE.md) rows for
`settings_report.py`, `matter_handlers/writable_attributes.py`, and
`matter_handlers/`.

Issues: #186 (shipped the `onTime` setting), #191 (the report that surfaced it),
#197 (ADR-0005's decision, and this ADR's follow-up spec review that
superseded it).

## For AI agents
- DO: check `NOT_SETTINGS` (not `COMMAND_FIELD_ATTRIBUTES`) before treating a
  writable attribute as excluded — the generated table is a detector, not the
  filter.
- DO: when `COMMAND_FIELD_ATTRIBUTES` gains a member CI didn't expect, file it
  into `NOT_SETTINGS` with a spec citation if it is genuinely inert, or into
  `REVIEWED_COMMAND_FIELD_COLLISIONS` with a citation if it is a real setting —
  never leave it in neither.
- DO: cite a specific spec section (Application Clusters or core) when adding to
  either list. "It shares a name with a command field" is evidence to *check*,
  not a reason on its own.
- DON'T: re-derive "a command-field match means not a setting" as a general
  rule. It was tried, reviewed against the spec, and found to hold for only one
  of its four founding members for that reason.
- DON'T: treat `quality: "N"` as a proxy for "is a setting" — it misclassifies
  46 writable attributes, not one.
- DON'T: declare `DoorLock.OperatingMode` a command parameter. It is a real
  setting; the collision with `SetHolidaySchedule`'s field of the same name is
  reviewed and recorded in `REVIEWED_COMMAND_FIELD_COLLISIONS`.
