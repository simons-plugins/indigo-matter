---
parent: Decisions
nav_order: 19
title: "ADR-0019: A real setting may be withheld from the settings report when offering it would break the plugin's own control path"

status: "accepted"
date: 2026-11-25
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0019: A real setting may be withheld from the settings report when offering it would break the plugin's own control path

## Context and Problem Statement

The settable-attribute report (`settings_report.py`, issue #191) lists writable
attributes a device implements and the plugin does not offer, and ends by inviting the
user to open an issue asking for them. [ADR-0006](0006-a-curated-exclusion-list-cross-checked-by-the-generator.md)
gave the report one exclusion list, `NOT_SETTINGS`, for attributes that are **not
settings at all**: a command's inert pre-set field, a momentary action wearing attribute
clothing, commissioner scratch space.

Issue #368 added a case that list cannot hold. `DoorLock.RequirePinForRemoteOperation`
(0x0033) **is** a genuine, user-facing setting. But the plugin sends LockDoor and
UnlockDoor without a PIN, so switching it on would make the lock refuse every command
Indigo sends and cut Indigo off from the lock. It must never be offered, and the report
must not invite a request for something the plugin will decline to build.

## Decision Drivers

* `NOT_SETTINGS` carries a claim ("this is not configuration") that is false here, and
  the explorer labels its members "command parameter".
* The reason is about this plugin's own behaviour, not about the attribute.
* The report is read by users and relayed to maintainers: an invitation to ask for a
  feature that will be refused wastes both.

## Considered Options

* Add the attribute to `NOT_SETTINGS`
* Add it to `INFRASTRUCTURE_CLUSTERS` or `DRIVEN_ATTRIBUTES`
* A separate `DELIBERATELY_NOT_OFFERED` table, each entry with its reason
* Leave it in the report and refuse the request when it comes

## Decision Outcome

Chosen option: "A separate `DELIBERATELY_NOT_OFFERED` table", in `settings_report.py`
beside `NOT_SETTINGS`. A real setting may be listed there when offering it would break
a path the plugin itself depends on, with the reason written as the value. The survey
skips these; the explorer still shows them as plain `[writable]`.

This **narrows ADR-0006 without editing it**: ADR-0006 stays the decision about what is
not a setting; this adds a second, independent reason for the report to stay quiet. The
two tables must stay disjoint, and neither may overlap a declared `DeviceSetting`.

### Consequences

* Good, because the reason is stated where the exclusion is, and `NOT_SETTINGS` keeps
  its meaning.
* Good, because the explorer stays complete (ADR-0004).
* Bad, because there is a fifth filter in `survey_node` (offered, driven, infrastructure,
  `NOT_SETTINGS`, this one) for a maintainer to keep in mind.

### Confirmation

`tests/test_settings_report.py`: the excluded attribute is never reported (with or
without an `offered` callback), a node holding only it reports nothing, the table is
disjoint from `NOT_SETTINGS`, `REVIEWED_COMMAND_FIELD_COLLISIONS` and `SETTINGS`, every
entry is a real writable attribute with a reason, and the explorer still shows it.

## More Information

Issue #368; PR #388. Current member: `DoorLock.RequirePinForRemoteOperation`. Reading
that attribute to explain a refused lock command is separate, unbuilt work.

## For AI agents
- DO: put an attribute here only with a written reason about the plugin's own behaviour.
- DON'T: put a not-a-setting attribute here (that is `NOT_SETTINGS`), or offer one that
  is listed here.
