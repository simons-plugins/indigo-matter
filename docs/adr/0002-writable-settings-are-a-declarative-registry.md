---
parent: Decisions
nav_order: 2
title: "ADR-0002: Writable device settings are a declarative registry, edited in the Edit Device dialog"

status: "accepted"
date: 2026-08-11
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0002: Writable device settings are a declarative registry, edited in the Edit Device dialog

## Context and Problem Statement

A *setting* is a writable Matter configuration attribute a user may want to
change — the Aqara FP300's presence hold time and motion sensitivity were the
first two. It is a different thing from a *reading* (which cluster handlers
already surface as Indigo states) and from a *control* (on/off, level, setpoint —
Indigo's built-in device actions).

Issue #85 set the first precedent: one custom Indigo action, one callback, one
ad-hoc bounds check, for one attribute. Several more settings were already
queued (#70 Pump Configuration, #71 Mode Select, #175 appliance modes, thermostat
config), and that shape scales badly — each new one re-implements the same
validation and, worse, the same read-back verification.

Two constraints made the naive answers unworkable:

* **Bounds cannot be guessed.** Enumerating the Matter data model showed that of
  57 writable attributes on the clusters this plugin handles, exactly **two** take
  their bounds from another attribute on the device. A registry that required a
  device-reported limits attribute would have excluded 55 of 57.
* **A firmware ACK is not evidence.** Aqara firmware acknowledges writes it then
  silently ignores, so success has to mean "a subsequent read returns the value we
  asked for", and that logic must exist exactly once.

## Decision Drivers

* Adding the *n*th setting must not cost a new validation path or a new
  verification path.
* No setting may ever be offered against a guessed range.
* The user must be able to open a device and **see what its settings currently
  are**, not merely set them blind.
* Indigo builds ConfigUI from static XML per device *type* with no runtime field
  injection, unlike device states (`getDeviceStateList`).

## Considered Options

* A bespoke custom action per setting (extending #85's precedent)
* One dynamic custom action with an attribute picker
* A declarative registry, with the UI in the device's Edit Device dialog
* A declarative registry, with the UI in a plugin-menu dialog

## Decision Outcome

Chosen option: "A declarative registry, with the UI in the device's Edit Device
dialog".

Each setting is a `DeviceSetting` declaration in `matter_handlers/settings.py`
naming its cluster, attribute, owning Indigo device types, and a **bounds
strategy** — `FromAttribute` (the device reports its own limits), `StaticRange`
(the spec fixes them) or `EnumValues`. The machinery around it is written once:
offer, seed, validate, write, verify.

**Placement reversed the original issue's own recommendation, and the reason is
concrete:** `uiPath="DeviceActions"` puts an action in the menu you use to *build*
a trigger, so there is nowhere to open a device and read its current settings. A
plugin-menu dialog fails for a different reason — ConfigUI has no per-field change
callback, and the only dynamic mechanism (`<List dynamicReload="true">`)
regenerates a list's *options*, so such a dialog physically cannot display a
current value.

#85's action survives, re-routed through the same verified path, because it does
a job the dialog cannot: changing a setting from an automation.

Adding a setting is therefore three steps: declare it here; add its ConfigUI
field(s) to the owning type in `Devices.xml`; declare a `<State>` whose id is the
setting's `key`. **No Python per setting** — which was the half that mattered.

### Consequences

* Good, because validation and read-back verification have exactly one
  implementation each, and every new setting inherits both.
* Good, because bounds are never invented: a setting whose bounds cannot be
  resolved is not offered at all.
* Good, because `device_settings.py` and `matter_handlers/settings.py` are
  Indigo-free and matter-server-free, so the case that matters most — firmware
  that ACKs and ignores — is unit-testable without hardware.
* Bad, because the ConfigUI XML **cannot be generated** from the declarations and
  is kept in step by hand. Mitigated by a parity test, not by discipline.
* Bad, because a setting's `key` is simultaneously the ConfigUI field id and the
  Indigo state id, so it must satisfy Indigo's strict state-id rules (ASCII
  letters and digits, leading letter, no underscores — an underscore raises
  `LowLevelBadParameterError` naming no key).
* Bad, because the dialog draws from cached state rather than reading live, so a
  value changed seconds earlier from another ecosystem can be stale. Accepted:
  opening a dialog runs on the Indigo UI thread and a read from a sleepy Thread
  device is seconds.

### Confirmation

`tests/test_plugin_module.py` enforces the registry↔XML parity in both
directions: every declared setting has its ConfigUI field, its `hasX` marker and
its `<State>`; and no orphan setting field exists in the XML without a
declaration. `tests/test_device_settings.py` covers the offer/seed/validate/write
paths, including a client that reproduces ACK-then-ignore.

## Pros and Cons of the Options

### A bespoke custom action per setting

* Good, because the first one is the least code.
* Bad, because each subsequent one re-implements bounds checking and read-back
  verification, and they will diverge.
* Bad, because an action cannot show the user what the setting currently is.

### One dynamic custom action with an attribute picker

* Good, because it is one callback for all settings.
* Bad, because it still lands in the Actions menu, so there is still nowhere to
  read current values — the objection that killed the option.

### A declarative registry in the Edit Device dialog

* Good, because settings appear where a user already goes to configure a device,
  with their current values visible.
* Bad, because it requires hand-maintained XML per device type.

### A declarative registry in a plugin-menu dialog

* Good, because no per-type XML would be needed.
* Bad, because a menu dialog cannot display a current value at all — no per-field
  ConfigUI callback exists. This is the same constraint that sends the pairing
  codes and the #191 diagnostics to the event log.

## More Information

Module-level description: [`../ARCHITECTURE.md`](../ARCHITECTURE.md) rows for
`device_settings.py` and `matter_handlers/`. Capability gating is
[ADR-0003](0003-attributelist-is-the-capability-authority.md). The generated
writable-attribute table is a build artefact because only the bridge node may
import `@matter/model` — that confinement is **workspace ADR-0006**, not a
decision of this ADR.

Issues: #85 (the precedent), #186 (this decision), #197 (one declaration —
`onTime` — turned out to target a command parameter and is being retired; that
does not affect the registry design).

## For AI agents
- DO: add a setting as a `DeviceSetting` declaration plus its `Devices.xml` field
  and `<State>`, and nothing else.
- DO: declare a bounds strategy for every setting; if bounds cannot be resolved,
  the setting must not be offered.
- DON'T: write a bespoke action or a second validation path for a new setting.
- DON'T: treat a write ACK as success — only a matching read-back is.
- DON'T: try to generate the ConfigUI XML; Indigo has no runtime field injection.
