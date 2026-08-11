---
parent: Decisions
nav_order: 5
title: "ADR-0005: A command parameter is not a setting"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0005: A command parameter is not a setting

## Context and Problem Statement

[ADR-0002](0002-writable-settings-are-a-declarative-registry.md) makes a setting a
declaration: a cluster, an attribute, a bounds strategy, the device types that
carry a field for it. [ADR-0003](0003-attributelist-is-the-capability-authority.md)
makes the device's own AttributeList the authority on whether a given unit
implements it. Between them they answer "is this attribute writable, and does this
device have it?" — and neither asks whether writing it *means* anything.

Issue #186 declared OnOff's `OnTime` (0x4001) as a setting labelled "Auto-off
after". It passed every check the design has. It is writable in the spec, the
GRILLPLATS implements it, the write was accepted, and a read-back returned the
value written. It has never once turned a plug off.

`OnTime` and `OffWaitTime` (0x4002) are the mandatory **fields of
`OnWithTimedOff`** (command 0x42) — a command this plugin has never implemented.
matter.js states the consequence in its own words at `OnOffServer.js:38-40`:
*"Writes never start a countdown — only `OnWithTimedOff` does."* So a pre-set
value does nothing at all. `off()` then zeroes `OnTime`, inside an
`if (this.features.lighting)` guard that every device carrying the attribute
satisfies, the attribute being conformance LT.

Live on the GRILLPLATS: a write of 100 verified by read-back, no auto-off, and
back to 0 as soon as the plug was switched.

The same mistake reached the diagnostics from the other end. Issue #191's
settable-attribute report named `OffWaitTime` on two plugs as a setting that
"COULD be added" — a recommendation to build something that cannot work.

So the question this ADR answers is: **what distinguishes a writable attribute
that is configuration from one that is not, and can that be decided mechanically
rather than per case?**

## Decision Drivers

* The failure is invisible to every check the plugin has. Read-back proves a write
  landed; nothing proves it meant anything.
* It surfaces in two independent places — the settings registry and the automatic
  report — so a fix in one is not a fix.
* The report is *automatic* and fires on hardware that is working perfectly. A
  false positive there sends a user to file an issue for a non-feature.
* `settings_report.INFRASTRUCTURE_CLUSTERS` already excluded `Identify` and
  `GeneralCommissioning` by hand, for reasons that were correct but unstated —
  the intuition existed before the rule did.

## Considered Options

* **The command-field rule** — a writable attribute that is also a field of a
  command on its own cluster is that command's parameter.
* **Filter on the model's `quality: "N"` (nonvolatile)** — configuration
  persists; a transient does not.
* **Hand-list the two OnOff attributes** beside the existing exclusions.

## Decision Outcome

Chosen option: **the command-field rule**, derived by the generator and emitted
into the checked-in table as `COMMAND_FIELD_ATTRIBUTES`.

The *rule* is positive evidence rather than a proxy: a command that takes the
attribute as a parameter is the thing that owns it. The *match* is by attribute
and field **name** within one cluster (see `parse_command_fields`), so a name
collision — a response-command field, or a struct member — is the theoretical
failure mode, and a false positive would silently demote a real setting. None
exists in the pinned model, where the rule selects **four** of 124 writable
attributes, all genuinely "writable but not configuration":

| Cluster | Attribute | Set by |
|---|---|---|
| `Identify` | `IdentifyTime` | `Identify()` |
| `GeneralCommissioning` | `Breadcrumb` | `ArmFailSafe()`, `SetRegulatoryConfig()` |
| `OnOff` | `OnTime` | `OnWithTimedOff()` |
| `OnOff` | `OffWaitTime` | `OnWithTimedOff()` |

Two of those four were already excluded by hand as "infrastructure". The rule
therefore generalises an intuition that was already load-bearing, and catches the
two it had missed.

Consequently:

* The `onTime` `DeviceSetting`, its `Devices.xml` field and its `<State>` are
  **retired**. No relabelling could have saved the field.
* The report skips command parameters, alongside infrastructure clusters and
  attributes the plugin already drives.
* The **explorer still shows them**, marked `[writable, command parameter]`.
  [ADR-0004](0004-matter-diagnostics-are-read-only.md) makes the explorer the
  complete surface; the report is the one allowed to be quiet.

**Matter has no auto-on**, which is worth stating because it is the obvious next
question. There is no `OffTime`; `OffWaitTime` is the *inverse* — a guard
preventing the device being turned back on during the window. Indigo already does
both directions natively, via `indigo.device.turnOn(id, duration=N)` and
`turnOff(id, duration=N)`.

### Consequences

* Good, because the exclusion is derived from the pinned model and updates itself
  when matter.js moves, rather than being a list someone must remember to extend.
* Good, because it documents *why* `IdentifyTime` was excluded, which until now
  was a comment rather than a rule.
* Good, because it is decided at generation time, so the runtime cost is a dict
  lookup.
* Bad, because removing the `onTime` **state** silently breaks any trigger bound
  to it — the asymmetry ADR-0003 exists to respect. Accepted because the setting
  shipped only in `v2026.10.1`, could only ever have held 0, and the removal is
  announced once in the event log rather than happening quietly.
* Bad, because the parse is fragile in two specific ways. A `Command`'s `Field`
  blocks are *sibling arguments* to its property object, so a brace scan finds
  none and yields an empty table that silently disables the filter — mitigated by
  the generator refusing to write a table missing any of the four known pairs. And
  a fixed-width search for a field's own object literal skips the multi-line
  list-typed form, which under-reports WITHOUT tripping that floor; caught in
  review of this PR, and now covered by `tests/test_enumerate_settings.py`.
* Bad, because the margin is thinner than "four of 124" suggests: **30** further
  attributes share a name with a command field on their own cluster and are
  excluded only by being read-only. The spec pattern — command-set attributes are
  made read-only, and these four are the exceptions — is what makes the rule work,
  and it is one `access` string away from not working.

### Confirmation

`pytest`, specifically: the four pairs are excluded from `survey_node`; retiring
the setting does **not** make `OnTime` appear as a gap (the two halves had to ship
together, because `offered_setting_pairs` was what suppressed it); the explorer
still shows them, marked; the generated set contains exactly the four; and no
declared `DeviceSetting` is one of them — the guard that would have stopped #186
shipping, mirroring the existing one for driven attributes.

The generator itself is tested against synthetic input in
`tests/test_enumerate_settings.py`, which runs in CI where `@matter/model` is
absent. `KNOWN_COMMAND_FIELD_ATTRIBUTES` only fires when a human regenerates the
table, so it is a floor for the operator, not coverage.

Live: with `matterSettingsSurveyed` cleared so every node re-surveys, both
Lighting-feature plugs report nothing at all.

## Pros and Cons of the Options

### The command-field rule

* Good, because a command that takes the attribute as a parameter is positive
  evidence that the command owns it — not a proxy for it.
* Good, because it is machine-readable from the model the plugin already parses.
* Neutral, because it changes real behaviour for only two attributes today; its
  value is that it keeps working.
* Bad, because matching by name means a collision is possible in principle. The
  generator's floor catches under-reporting; nothing catches a false positive
  except the report's exact-set test failing when the model moves.
* Bad, because it needs a balanced-parenthesis scan the existing parser did not
  have.

### Filter on `quality: "N"`

* Good, because "configuration persists" is a true generalisation.
* Bad, because it drops **29** attributes on that heuristic.
* Bad, because it wrongly demotes `SmokeCoAlarm.SmokeSensitivityLevel`, which
  plainly is configuration and simply is not marked `N` in the model. A rule that
  is wrong about a real setting is worse than the false positive it removes.

### Hand-list the two OnOff attributes

* Good, because it is three lines and carries no parser risk.
* Bad, because it is a hand list of something exactly derivable, and it would
  silently miss any command parameter a future matter.js introduces.

## More Information

The lesson that generalises past this bug: **read-back verification proves a write
LANDED, not that it MEANS anything.** ADR-0002's design can catch a setting the
device physically rejects and can never catch one that is semantically wrong.
Nothing in this repo can. Deciding an attribute is configuration is a modelling
judgement, and this ADR moves one part of that judgement — "is a command already
in charge of it?" — out of judgement and into the generator.

Registry design is [ADR-0002](0002-writable-settings-are-a-declarative-registry.md);
capability gating is [ADR-0003](0003-attributelist-is-the-capability-authority.md);
the read-only diagnostics rule is
[ADR-0004](0004-matter-diagnostics-are-read-only.md). The generated table is a
build artefact because only the bridge node may import `@matter/model` —
**workspace ADR-0006**, not a decision of this ADR.

Issues: #186 (shipped the setting), #191 (the report that surfaced it), #197
(this decision).

## For AI agents
- DO: check `COMMAND_FIELD_ATTRIBUTES` before declaring any new `DeviceSetting` —
  if the attribute is in it, the answer is an action that sends the command, not a
  setting.
- DO: regenerate `matter_handlers/writable_attributes.py` with
  `tools/enumerate_settings.py --emit-table` whenever the pinned `@matter/model`
  moves, and check the diff.
- DON'T: declare `OnOff.OnTime` (0x4001) or `OnOff.OffWaitTime` (0x4002) as
  settings. They were tried, shipped, and proved inert against real hardware.
- DON'T: hide command parameters from the explorer, or filter the report on
  `quality: "N"` instead — both were considered and rejected above.
- DON'T: treat a successful write plus a matching read-back as evidence that a
  setting works. It is evidence the attribute is writable and nothing more.
