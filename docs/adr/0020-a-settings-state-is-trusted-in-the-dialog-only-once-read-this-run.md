---
parent: Decisions
nav_order: 20
title: "ADR-0020: A setting's state is trusted in the dialog only once it has been read from the device this run"

status: "proposed"
date: 2026-11-25
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0020: A setting's state is trusted in the dialog only once it has been read from the device this run

## Context and Problem Statement

[ADR-0002](0002-writable-settings-are-a-declarative-registry.md) accepted that the Edit
Device dialog draws current values from the device's Indigo **states**, never a live read,
because a read from a sleepy Thread device takes seconds on the UI thread. That accepted a
stale value (a change made seconds earlier from another ecosystem). It did not consider a
state that was never read at all: Indigo initialises a new Integer state to 0
([ADR-0003](0003-attributelist-is-the-capability-authority.md), #190), so before a
device's value arrives the dialog showed 0 as if it were the device's. For a lock's
auto-relock that reads as "off" on a lock that relocks after 60 seconds, and typing 0 was
silently skipped because 0 equalled the default.

## Decision Drivers

* A default that looks like a value is worse than no value.
* The dialog must stay non-blocking (ADR-0002).
* "Unreachable right now" is not "never read": the last reported value is still the
  best available and was shown before this decision.

## Considered Options

* Keep trusting the state (ADR-0002 as accepted)
* Read live when the dialog opens
* Trust a setting's state only once a device reading has been written this run

## Decision Outcome

Chosen option: "Trust a setting's state only once a device reading has been written this
run". `DeviceSync` records, in memory, each `(device, setting)` pair whose state has been
written from a device reading (a prime, a live update, or a verified write read-back). Until
then the dialog seeds the field blank (a picker leads with a "not read yet, leave
unchanged" row), `planned_writes` treats the previous value as unknown so any entered
value is written and verified, and the state's display is "unknown". A write whose
read-back is unanswered or null makes the setting unread again, and its cached reading
is dropped so a later restart of the device cannot re-vouch for it. A snapshot of an
unavailable node still counts as a reading (last values reported). So does a null for a setting that declares a null state (PR #387's "previous state"): it is the device's answer, not an absence of one.

This **narrows ADR-0002's acceptance of cached state** without editing it: the dialog
still never reads live, it just refuses to treat an unread default as a value.

### Seeds, and what "untouched" means

The dialog remembers, per device, the values it was seeded with when it opened. At Save a
field equal to its seed (numerically for a number field, so "030" is "30") is untouched,
even if the device's value has since moved (another app), so an untouched Save never
overwrites it. "Previous value" for the log and the verification is still the current
state. If no seed is available (the plugin restarted with the dialog open, the dialog's
build failed, or a second dialog was opened on the same device so the slot is ambiguous),
nothing is called an edit: no setting is written, validation does not judge the fields,
and the log asks the user to reopen the dialog. The consequence is that a user cannot force
a resend of the value the dialog opened with; they change it and change it back.

A remembered seed older than ten minutes, found when another dialog opens on the same
device, is treated as abandoned (the client crashed before the close callback) and replaced,
with an INFO line; a younger one still makes the slot ambiguous. The replaced slot's seeds
are kept on the new one as superseded (at most three): the old dialog may still be open, and a
field of it saved untouched still equals its old seed, so it is not written (the log names the
device and setting and asks the user to reopen). A value that differs from every seed is a
real edit and is written. Expiry is judged only at
open, never on save, so one dialog left open for an hour still saves.

An edit that cannot be sent (plugin not connected, or the device flagged unreachable) is
refused in validation with a field error rather than failing after the dialog has closed.

### Failures are recorded on the setting, not on the device

The Indigo device error flag means "unreachable" and nothing else: a failed or unverified
setting write never sets or replaces it. The failure is recorded on the String state
`settingsError` (one short sentence per setting naming its label and cause, joined with
"; "), written without clearing the device's error flag and with an explicit uiValue. The
failed setting's own state keeps the device's real value (the read-back, else the previous
value), never the requested one. A setting's sentence is cleared by that setting's next
successful write, or by a later device reading of the value the user asked for (remembered
in memory only); unrelated reports do not clear it, and after a restart only a good write
does.

### Consequences

* Good, because 0 is no longer indistinguishable from "unread".
* Good, because it is one generic mechanism for every setting.
* Bad, because there is a second piece of per-device in-memory state to keep honest: a
  re-prime must cover only unread settings, never clear the error flag, and the cached
  readings must be kept current by every state write.
* Bad, because an unread picker needs an extra row (a menu cannot be blank).

### Confirmation

`tests/test_unread_settings.py`, `tests/test_lock_settings.py`, `tests/test_device_sync.py`.

## More Information

Issue #368, PR #388. A contributor fork not yet on this remote is known to carry its own
ADR-0019, so numbers may need changing at merge.

## For AI agents
- DO: seed a setting field blank until it has been read this run.
- DON'T: seed from a state that no device reading has written.
