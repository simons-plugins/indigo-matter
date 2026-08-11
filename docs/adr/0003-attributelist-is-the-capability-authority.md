---
parent: Decisions
nav_order: 3
title: "ADR-0003: A device's own AttributeList is the only capability evidence"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0003: A device's own AttributeList is the only capability evidence

## Context and Problem Statement

Deciding whether to offer a setting, or to give a device a state, requires
knowing whether that unit implements the underlying attribute. Three tempting
sources of that answer are all wrong:

* **A reading.** Indigo supplies a default (0) for every declared state, and
  matter-server caches whatever it once saw. A value coming back is not evidence
  the attribute exists.
* **The presence of a limits attribute.** This stood in for a capability check
  while both shipped settings happened to have one. Most settings take their
  bounds from the spec, so "are the limits readable?" cannot answer "does this
  unit have the attribute?" — it would have excluded 55 of 57 candidates.
* **The product name.** Issue #190 asserted that the Shelly 1PM Mini Gen3
  implements OnOff's Lighting feature and the IKEA GRILLPLATS does not. **The live
  devices are the exact opposite.** That error survived into the issue text, into
  the test fixtures, and into a shipped release before real hardware caught it.

## Decision Drivers

* An error of this kind is invisible: fixtures written from the wrong belief pass
  against a parser that cannot read the real device.
* The two consumers of the answer have opposite failure costs. Withdrawing a
  device **state** breaks any trigger or control page bound to it, silently, and
  is visible to the whole server. Withholding a dialog **field** is recoverable —
  the user sees something they cannot set, and nothing else breaks.
* A partial interview is normal, not exceptional: state lists are built at device
  start, AttributeLists are cached at reconcile, so there is always a window in
  which nothing is known.

## Considered Options

* Infer capability from readings or cached values
* Infer capability from device type / product name / firmware version
* Use the device's own AttributeList (0xFFFB), with one uniform policy for unknown
* Use the AttributeList, with a **deliberately asymmetric** policy for unknown

## Decision Outcome

Chosen option: "Use the AttributeList, with a deliberately asymmetric policy for
unknown".

The device's `AttributeList` (0xFFFB) per cluster is the authority. It is what
the device says it has, rather than what the spec says it might, and unlike a
limits attribute it exists on every cluster.

`settings.implements()` returns **True / False / None**, and **None is not
False**. There is deliberately **no single policy** for what None means:

* `unimplemented_states` — a state is withdrawn only on a **positive NO**.
  Unknown keeps the state, because removing one breaks bound triggers.
* `offered_settings` — a spec-bounded setting is **withheld** without positive
  evidence, because `StaticRange`/`EnumValues` resolve unconditionally, so
  "unknown" would silently become "offer it to everything".

Assuming the first policy held everywhere is exactly how the #192 cache-eviction
path came to believe it could never hide a setting.

Corollaries that follow from the same rule and must not be re-derived:

* **An empty `attributes: {}` snapshot is "no information", never "implements
  nothing".** matter-server populates its attribute cache lazily and the first
  `node_updated` after a server restart legitimately carries nothing. Believing it
  would retract capabilities on every routine restart.
* **A list that could only be partly parsed supports no confident NO.** Claiming
  absence from data known to have failed parsing is worse than admitting
  ignorance.
* Cache eviction is **not** a uniform policy — it depends entirely on what the
  empty case is read to mean. `_attribute_lists` absent = unknown = safe;
  `_power_source_eps` absent is a positive "one power source" and must never be
  evicted.

### Consequences

* Good, because capability claims are now machine-derived from the device rather
  than transcribed by a human — which is precisely what #190 got wrong.
* Good, because a partial interview under-reports instead of inventing.
* Bad, because the asymmetry is genuinely confusing and invites a "tidy-up" that
  makes the two policies uniform. It is documented in the code and here for that
  reason.
* Bad, because a device that never reports an AttributeList for a cluster will
  never be offered its spec-bounded settings, even if it does implement them.
  Accepted: silently offering to everything is the worse failure.

### Confirmation

`tests/test_device_settings.py` covers all three states of `implements()` for both
consumers, including that the state gate is deliberately weaker than the offer
gate. `tests/test_device_sync.py` covers the empty-snapshot case end to end.

## Pros and Cons of the Options

### Infer capability from readings or cached values

* Good, because it needs no extra data.
* Bad, because Indigo's own state defaults make every attribute look present.

### Infer from device type / product name / firmware

* Good, because it reads naturally in an issue or a commit message.
* Bad, because it is a human transcription step, and #190 proves it fails
  silently and propagates into fixtures.

### AttributeList with one uniform policy for unknown

* Good, because it is easy to reason about and easy to test.
* Bad, because the two consumers have opposite failure costs — any single policy
  is wrong for one of them.

### AttributeList with an asymmetric policy

* Good, because each consumer gets the conservative answer for *its* failure mode.
* Bad, because the asymmetry must be explained every time someone reads the code.

## More Information

Implemented in `matter_handlers/settings.py` (`implements`) and consumed by
`device_settings.py` (`offered_settings`, `unimplemented_states`) and
`device_sync.py`. Registry design is
[ADR-0002](0002-writable-settings-are-a-declarative-registry.md); the read-only
diagnostics built on the same evidence are
[ADR-0004](0004-matter-diagnostics-are-read-only.md).

Issues: #190 (states follow AttributeList; the wrong-attribution incident), #192
(cache eviction), #186 (where the capability check originated).

## For AI agents
- DO: call `implements(attribute_list, attribute)` and handle True / False / None
  as three distinct cases.
- DO: treat an empty attribute snapshot as "no information".
- DON'T: infer a capability from a state value, a cached reading, a limits
  attribute, a product name, or a firmware version.
- DON'T: make the state policy and the offer policy uniform — the asymmetry is
  deliberate and load-bearing.
- DON'T: state in an issue or commit which real product implements what unless
  the claim came from that device's own AttributeList.
