---
parent: Decisions
nav_order: 4
title: "ADR-0004: The Matter attribute diagnostics are read-only, permanently"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0004: The Matter attribute diagnostics are read-only, permanently

## Context and Problem Statement

Issue #191 added two diagnostics: an automatic report of the settable Matter
attributes a device implements that the plugin does not offer, and a power-user
explorer that dumps a chosen node/endpoint/cluster to the event log. Both exist
because adding support for a device's settings requires knowing what it exposes,
and the only previous way to find out was to dump `get_node` by hand and read the
JSON — a step nobody but the maintainer could repeat.

The original issue left a generic attribute **writer** open — "if included at
all". Once an explorer can address `(node, endpoint, cluster, attribute)`, adding
a value field is a small change, so the question would be re-raised by every
future reader unless it is settled.

## Decision Drivers

* These devices are typically joined to two or more ecosystems at once. A write
  is device-global — it changes behaviour in Apple Home and everywhere else.
* The plugin already has a validated write path, and its entire value is that
  every write is bounds-checked against a declared strategy and confirmed by
  read-back.
* A diagnostic's worth is that its output is trusted. Anything that can change
  the system it is measuring is a different kind of tool.

## Considered Options

* Include a generic attribute writer in the explorer
* Include a writer, gated behind a confirmation or an "advanced" flag
* Read-only, permanently, with writes only via a declared `DeviceSetting`

## Decision Outcome

Chosen option: "Read-only, permanently, with writes only via a declared
`DeviceSetting`". **This is settled, not deferred. Do not re-propose it.**

Three reasons, none of which will change:

1. **A generic write is unvalidated by construction.** The whole point of the
   settings registry ([ADR-0002](0002-writable-settings-are-a-declarative-registry.md))
   is that every write declares where its permitted values come from and is
   verified by read-back. A free-text attribute writer has neither, which makes it
   the one path that can put a device into a state the plugin cannot explain
   afterwards.
2. **It is device-global and multi-admin.** An unvalidated write from a
   diagnostic screen changes behaviour in every ecosystem the device is joined to,
   with no undo.
3. **It adds no capability.** The settings dialog already covers every write for
   which a validated bounds strategy exists, and extending *that* registry is the
   correct way to add another — one declaration, inheriting bounds checking and
   verification.

If a future need appears, the answer is a new `DeviceSetting` declaration in
`matter_handlers/settings.py`, never a generic writer.

**Enforcement is a source-level test**, not a convention: `test_diagnostics_menu.py`
asserts that `MatterWrite`, `.write(`, `send_command` and `MatterCommand` appear
nowhere in `diagnostics_menu_mixin.py`. The point is not that today's code happens
not to write; it is that no future edit quietly adds one.

**Output goes to the event log**, which is forced rather than chosen: Indigo's
ConfigUI has no per-field change callback and `<List dynamicReload="true">`
regenerates a list's *options*, so a menu dialog physically cannot display a
result. Same constraint and same resolution as the pairing codes.

The explorer does offer **one live read** (`MatterClient.read`) alongside the
cached `get_node` dump, because the two are not interchangeable and the user must
say which they want. That is a read; it does not weaken this decision.

### Consequences

* Good, because the diagnostic can be trusted not to have changed what it
  reported.
* Good, because it keeps a single, verified write path in the plugin.
* Bad, because a maintainer debugging an exotic device cannot poke an attribute
  from the UI and must add a declaration (or use another ecosystem's tooling)
  instead. Accepted deliberately.
* Bad, because the source-level test is a blunt instrument: a legitimate future
  need to *read* via a differently-named client method could trip it. Preferable
  to a rule nothing enforces.

### Confirmation

`tests/test_diagnostics_menu.py::test_the_diagnostics_never_write_to_a_device`
fails if any write vocabulary enters the module.

## Pros and Cons of the Options

### Include a generic attribute writer

* Good, because it makes the explorer a complete debugging tool.
* Bad, because nothing declares the bounds, so nothing can validate the value.
* Bad, because a bad write is device-global across every joined ecosystem.

### Include a writer behind a confirmation or "advanced" flag

* Good, because it puts the risk in front of the user.
* Bad, because a confirmation dialog is not validation — it moves responsibility
  without adding evidence, and the failure it guards is silent and later.

### Read-only, writes only via a declared `DeviceSetting`

* Good, because every write in the plugin keeps its bounds and its read-back.
* Good, because adding a setting is already cheap (one declaration + XML).
* Bad, because there is no ad-hoc escape hatch.

## More Information

Module description: [`../ARCHITECTURE.md`](../ARCHITECTURE.md) rows for
`diagnostics_menu_mixin.py` and `settings_report.py`. Capability evidence is
[ADR-0003](0003-attributelist-is-the-capability-authority.md).

Issue #191. Follow-up #197 refines *what* the report names (attributes that are
parameters of an unimplemented command are not settings) — it does not touch the
read-only decision.

## For AI agents
- DO: add a `DeviceSetting` declaration when a device attribute needs to become
  writable.
- DO: keep the diagnostics' output on the event log — a ConfigUI dialog cannot
  display a result.
- DON'T: add `MatterWrite`, `send_command` or any write path to
  `diagnostics_menu_mixin.py` or `settings_report.py`.
- DON'T: re-propose a generic attribute writer, gated or otherwise. This was
  decided on 2026-08-11 and the three reasons are permanent.
