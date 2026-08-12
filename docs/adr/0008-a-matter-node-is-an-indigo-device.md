---
parent: Decisions
nav_order: 8
title: "ADR-0008: A Matter node is an Indigo device, and the root of its endpoint devices' group"

status: "accepted"
date: 2026-08-12
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0008: A Matter node is an Indigo device, and the root of its endpoint devices' group

## Context and Problem Statement

Matter is node → endpoints → clusters, and endpoint 0 is the root: the spec
calls it *"a 'read me first' endpoint that describes itself and the other
endpoints that make up the node"* and forbids application device types on it.
This plugin maps **one Indigo device per endpoint per handler**, and endpoint 0
gets **nothing** — `device_sync.py`'s `_unknown_spec` returns `None` for it, on
purpose, because there is no handler and no device to build. So an FP300 is one
node and four Indigo devices, and everything endpoint 0 carries — identity,
firmware, reachability, battery, diagnostics, localization, identify, OTA — is
homeless.

Costs already paid for that absence, before this PR:

* The battery fan-out heuristic ("PowerSource lives on a different endpoint
  than the device it augments, so route by hand") used to be open-coded at
  four separate sites in `device_sync.py` — the creation prop, priming fan-in,
  the reconcile self-heal, and live fan-out — before issue #205 collapsed them
  into the single `resolve_power_coverage`/`_battery_targets` seam. **Issue #82
  was a cross-contamination bug in one of those four copies.** #205 fixed the
  routing; it did not give the source cluster's OWN endpoint (0) anywhere to
  live, because there was still no device there to route to.
* `SurveyLog` (issue #191) is a JSON blob in `pluginPrefs`, keyed by node —
  `settings_report.py`'s own comment says why: *"a node is not an Indigo
  device (an FP300 is one node and four devices)"*. It is a node object,
  emulated in prefs because there is nowhere else to put it.
* The Matter node id is copied into every one of a node's Indigo devices'
  `address` prop (issue #18), and `mark_unreachable(node_id)` loops over N
  devices to say the same thing N times, because there is no single device
  that could say it once.
* Multi-endpoint node names carry `"(endpoint N)"` suffixes to disambiguate
  siblings that would otherwise collide — a symptom of naming individual
  endpoint devices as if one of them were also the node, which none of them is.
* Node-level settings are **unimplementable**: the localization clusters
  (issue #204's motivating case) carry real, device-reported-bounds settings,
  and `settings_report.py`'s own generated output now says so in its output —
  there is no device to host a ConfigUI field for them.

## Decision Drivers

* **Per-endpoint devices must stay.** In Indigo the *device* is the
  automation unit — triggers, control pages, and every `indigo.device.*`
  action bind to it. In Home Assistant the *entity* is. This is a genuine
  Indigo/HA asymmetry, not a preference: collapsing an FP300 into one device
  would force `type="custom"` and throw away native sensor/relay/dimmer
  behaviour Indigo gives for free. "Copy Home Assistant" is not automatically
  right here, and this ADR does not do it.
* **[ADR-0003](0003-attributelist-is-the-capability-authority.md): a device's
  own AttributeList is the only capability evidence.** A node device is not
  exempt — it must be gated on ep 0's own BasicInformation AttributeList
  exactly like every other capability decision in this plugin, and "unknown"
  must not become "yes".
* **[ADR-0007](0007-a-retired-everywhere-setting-keeps-its-state-flagged.md):
  a shipped State is permanent.** Whatever states this ADR puts on the new
  device type, the list must be deliberately short — every one shipped is a
  state some future user's trigger can bind to and this plugin can then never
  cleanly remove.
* **Migration must be idempotent and must never rename a fielded device.**
  Grouping existing endpoint devices under the new node device, and truing up
  names now that a node-level name exists to anchor them, are both deferred to
  a follow-up PR. This PR creates a device; it renames nothing and groups
  nothing that already exists.

## Considered Options

* A — a synthetic node device only, ungrouped
* B — grouping only (elect an existing endpoint device as the node's root)
* A+B — a synthetic node device that is ALSO the group's root (chosen; A now,
  B in the follow-up PR)
* C — a `<DeviceFactory>` covering the whole node
* D — collapse a node to one custom device, Home-Assistant-shaped

## Decision Outcome

Chosen option: **A+B — a synthetic node device, which becomes the root of its
endpoint devices' Indigo device group.** This PR is A only: it creates the
device. Grouping the existing per-endpoint devices under it is real,
non-trivial work — deciding what happens to a fielded device's current
folder/name, making the operation idempotent against a partially-grouped
node, ordering group membership changes safely — and is deferred to the
next ADR-governed PR, exactly as issue #204 scoped it.

A synthetic `matterNode` Indigo device is created for endpoint 0 of every node
this plugin has enough evidence to describe, gated on ep 0's own
BasicInformation AttributeList (ADR-0003) rather than created unconditionally.
It carries identity (vendor/product/firmware), reachability, and — only when
the node's own PowerSource coverage says endpoint 0 is powered by it — a
battery reading, using Indigo's own reserved `batteryLevel` state name so
that once B lands, `getGroupList()`'s documented behaviour (*"some
properties, such as `batteryLevel`, only exist on the main/root device"*)
applies to it for free.

### Why C (DeviceFactory) is rejected

A `<DeviceFactory>` looked attractive — `getDeviceFactoryUiValues(dev_id_list)`
runs on open with the group's membership already in hand, which sounds like
exactly the node-scoped, seedable dialog issue #204 wants for later
node-level settings. It is rejected because it is a **one-way door**: the
canonical guidance is explicit — *"it's not possible to have a Device Factory
implementation and individual plugin device definitions together."* Adopting
it would remove all 23 existing `<Device>` types from the New Device dialog in
one move, for every user, on every device this plugin has ever created.
Surveying installed plugins in this workspace, only `ShellyNGMQTT` uses a
Device Factory, and its own field notes (workspace memory
`shelly-htg3-ng-component-save.md`) record the UX cost directly: newly created
child devices are **blank until each is individually opened and saved**. That
is a worse creation experience than this plugin already has, in exchange for
a capability — `allowUserCreation="false"` — this plugin already gets for
free from a plain `<Device>` type (Indigo 2022.1.2 / API 3.1), with none of
the one-way commitment.

### Why D (collapse to one device) is rejected

Restated from the per-endpoint-devices decision driver above: it destroys
native Indigo device behaviour (relay/dimmer/sensor actions, control-page
widgets, `indigo.device.*` calls) for every existing endpoint device on every
multi-endpoint node this plugin has ever created, to solve a problem — ep 0
has nowhere to live — that does not require touching any of them.

## Cross-controller evidence

Four independent Matter controller implementations were checked against
source or vendor docs, and all four give endpoint 0 a home distinct from any
application endpoint:

| System | Node object | Where ep-0 clusters go |
|---|---|---|
| **Home Assistant** | one HA *device* per node today (`MatterNodeDevice`; `get_device_id` returns `{fabric}-{node}-MatterNodeDevice` for every non-bridged endpoint, collapsing the whole node to one device) | on that device, with no per-endpoint fan-out code anywhere |
| **HA, agreed future direction** | migrating from "one device per node" to "parent device + sub-device per endpoint" — by **adding** a parent, not by removing the node device (architecture discussion #1036, migration tracked in `home-assistant/core#172449`, open) | *"The parent device will have entities representing the state of the power strip … network connection status and firmware update"* |
| **SmartThings** | one device record; `endpoint_to_component()` returns `"main"` for anything unmapped | ep 0 emits on the `main` component, the same component every other unmapped endpoint attribute defaults to |
| **Hubitat** | parent device + component child devices | ep-0-shaped data (identity, network status) is the parent's |
| **openHAB** | one Thing per node, one channel group per endpoint **including 0** | identity properties (vendor, product, firmware) are Thing-level properties, not hidden in a channel group |
| **Apple Home** | one accessory per node | Accessory Information, Identify, and Battery services sit on the accessory itself |
| **Alexa** (via matter.js bridging) | a "head" device carries node-level identity | matter.js's own field notes are explicit: *"if you want to add power source info then it needs to be at the BridgedNodeEndpoint level"* — i.e., node-level, not any one child's |

Four-plus independent implementations converged on the same shape: endpoint 0
gets a home distinct from any application endpoint. **indigo-matter is
currently the outlier** — the only one of these with per-endpoint objects and
no node object at all — and Home Assistant's own migration direction is
telling: it is moving *toward* this plugin's per-endpoint shape by **adding**
a parent, not moving away from per-endpoint devices by removing them. That is
independent confirmation of both halves of this ADR's decision driver: keep
the per-endpoint devices, and give the node a home alongside them.

## Why the root is deterministic and needs no user designation

Contrast this with `autolog`'s zigbee2mqtt plugin (workspace memory
`autolog-jon.md`), where the user must hand-nominate a device's "main"
component: Zigbee has no protocol-level answer to "which endpoint is this
device's root", so a human has to supply one, and different users of the same
hardware can reasonably disagree.

Matter answers this question itself. Endpoint 0 always exists, on every
Matter node, exactly once, and the spec is unambiguous that it is the node's
own root — nothing to elect, nothing for a user to configure, and nothing
that could disagree between two installs of the same device. This ADR
deliberately never elects an *application* endpoint as the root instead:
there is no defensible way to prefer, say, an FP300's presence-sensing
endpoint over its temperature endpoint as "the real device", and any such
choice would be exactly the kind of human transcription step ADR-0003's
Decision Drivers already warn against (the #190 wrong-attribution incident).
Endpoint 0 sidesteps the question entirely by being the one endpoint that was
never a candidate for "which application function is the real one" in the
first place.

## ADR-0002's aside, noted and left moot

[ADR-0002](0002-writable-settings-are-a-declarative-registry.md)'s "Pros and
Cons" section rejected a plugin-menu dialog for settings with the reasoning
*"a menu dialog cannot display a current value at all — no per-field ConfigUI
callback exists."* Its **conclusion** stands; that one stated reason is
inaccurate on existence and unverified on behaviour, and this ADR records the
facts without adjudicating it:

* The callback exists. Indigo 2025.2's own `plugin_base.py` line 1199 defines
  `getMenuActionConfigUiValues` (verified on the live server, 2026-08-12),
  and the workspace's field notes (`plugin-dev/concepts/plugin-lifecycle.md`)
  document it on every supported version.
* Whether values it seeds actually *display* on every open is unverified:
  the canonical menu-items reference
  (`reference/canonical/plugin-dev/reference/xml/menuitems.md`) describes a
  menu dialog that "runs as if it's the first time the menu item is run
  (default values will always be used if present)" and never mentions the
  callback, and no plugin in the surveyed corpus was seen using it. A docs
  omission is not evidence of absence (this repo has been burned by exactly
  that before), but nobody has run the live experiment either.

What this ADR does is make the question **moot** for node-level settings,
not answer it a different way: the node device (`matterNode`) gives them the
same Edit Device dialog every other setting already uses —
`getDeviceConfigUiValues` (`plugin.py`), which demonstrably exists and is
already in production use seeding the settings section of every existing
device type. A node-level setting attaches to `matterNode` through the same
ADR-0002 registry, with one more owning type, and gets the same
current-value-seeded dialog for free — no menu dialog, seedable or not, ever
needs to enter the picture.

This is additive, **not** a supersession — ADR-0002 is immutable and this
does not edit it. Whether and how to actually add node-level settings there
is future work, not decided by this ADR.

## Note on ADR-0003's closing example

ADR-0003's body cites `_power_source_eps` as the
example of a cache that must not be evicted uniformly. Issue #205 (after
ADR-0003 was accepted) replaced that cache with `_power_coverage`, keyed and
read differently (see `device_sync.py`'s row in `ARCHITECTURE.md`). The
*example* in ADR-0003 is stale; the *decision* — AttributeList is the only
capability evidence, and eviction policy is not uniform, it depends on what
"absent" means for that specific cache — stands unchanged and is exactly what
this ADR's own ep-0 AttributeList gate relies on. Recorded here, not edited
into ADR-0003, because ADRs are immutable once accepted.

## Decision Outcome — consequences

* Good, because node-level settings become implementable through the
  existing ADR-0002 registry, with `matterNode` as one more owning type — no
  new settings machinery, only a new place for the existing machinery to
  attach to.
* Good, because `SurveyLog` and the issue #191 diagnostics gain a real target
  device instead of living only in `pluginPrefs`.
* Good, because `sw_version` — parsed by `matter_model.parse_node` since the
  very first version of this plugin — becomes visible in Indigo for the first
  time; nothing has ever surfaced it before this.
* Good, because once grouping (option B) lands in the follow-up PR, battery
  gets Indigo's own conventional home — the group's root device — instead of
  a heuristic fan-out onto whichever sibling happened to need it.
* Bad, because every node with enough evidence to pass the ADR-0003 gate now
  gets **+1 Indigo device**. This is the direct, accepted cost of giving
  endpoint 0 somewhere to live.
* Bad, because `indigo.device.delete` refuses to delete the root of a
  non-empty Indigo device group — harmless today (no grouping exists yet:
  option B is the follow-up PR), but load-bearing the moment it lands, so
  `delete_node`'s ordering (children before the node device) is established
  in THIS PR rather than left for the grouping PR to discover the hard way.
* Bad, because every already-fielded install needs a migration: the first
  informative reconcile after upgrade creates one new device per
  already-commissioned node that clears the ADR-0003 gate. This is additive
  (no existing device is touched, renamed, or moved) but is still a
  first-run behaviour change worth naming plainly.

### Confirmation

The Python test suite: `tests/test_device_sync.py` (node-device creation, the
ADR-0003 gate, idempotence, the empty-plan carve-out, the anti-mass-rename
guarantee for existing endpoint devices, `delete_node` ordering, the
tombstone/menu-recreate round trip, reachability), `tests/test_handlers.py`
(`BasicInformationHandler` unit tests), and `tests/test_plugin_module.py`
(`matterNode`'s `Devices.xml` shape, and that it is the only type carrying
`allowUserCreation="false"`). `python3 -m pytest -q` green is this PR's own
confirmation; a live-hardware check against the jarvis fabric is deferred to
the same pass that validates the grouping follow-up PR, because grouping is
what makes the node device's presence visible in the Indigo UI in a way
distinct from "one more device in the list".

## Pros and Cons of the Options

### A — synthetic node device only, ungrouped

* Good, because it is the minimum change that gives endpoint 0 a home at all.
* Neutral, because it does not yet deliver the battery-on-root-device
  behaviour `getGroupList()` documents — that needs option B too.
* Bad on its own, because a node device that is never the root of anything is
  a smaller, less useful version of what every other controller in the
  cross-controller survey actually does.

### B — grouping only

* Good, because it would give existing endpoint devices a common root without
  adding a new device type.
* Bad, because Indigo's grouping needs a root device to elect, and no
  existing endpoint device is a defensible choice (see "why the root is
  deterministic" above) — so B alone still needs *something* new to serve as
  root, which is most of what A already is.

### A+B — synthetic node device as group root (chosen; B deferred)

* Good, because it matches every surveyed controller's shape and gives
  `getGroupList()`'s root-device behaviour (battery, and whatever else
  Indigo reserves for a group root) somewhere real to land.
* Good, because splitting A and B into separate PRs keeps this PR's risk
  surface to "one new device type, additive only" — no renaming, no grouping
  bugs, nothing that could disturb a fielded device's identity.
* Bad, because until B ships, the new device is not yet doing the
  root-of-a-group job it exists to eventually do — an intermediate state,
  accepted for the sake of shipping A safely first.

### C — DeviceFactory

* Good, because it is node-scoped and seedable in one dialog, which fits
  issue #204's original framing of the node-settings problem well.
* Bad, because it is a one-way door: `<DeviceFactory>` and individual
  `<Device>` definitions cannot coexist, which would remove all 23 existing
  types from the New Device dialog for every user.
* Bad, because the one plugin in this workspace that uses it
  (`ShellyNGMQTT`) has documented field evidence of a worse creation UX
  (children blank until opened and saved) than what `allowUserCreation="false"`
  already gives this plugin for free, with none of the lock-in.

### D — collapse to one HA-shaped device

* Good, because it would make the Matter node ↔ Indigo device mapping
  trivially 1:1, simplest to reason about in isolation.
* Bad, because it destroys native Indigo device behaviour for every existing
  multi-endpoint node, which is the asymmetry this ADR's first decision
  driver exists specifically to protect.

## More Information

Builds on [ADR-0002](0002-writable-settings-are-a-declarative-registry.md)
(additive correction, not supersession), [ADR-0003](0003-attributelist-is-the-capability-authority.md)
(the creation gate; also the source of the stale-example note above), and
[ADR-0007](0007-a-retired-everywhere-setting-keeps-its-state-flagged.md) (why
the new type's state list is deliberately short). Module-level description:
[`../ARCHITECTURE.md`](../ARCHITECTURE.md) rows for `device_sync.py` and
`matter_handlers/`.

Issues: #204 (this PR — the node device only; grouping and renaming are the
follow-up), #205 (the battery-coverage machinery this ADR's battery state
reuses), #82, #190, #191, #18, #58 (the pre-existing costs this ADR pays
down), #197 (ADR-0007's context, cited for the "a shipped State is
permanent" decision driver).

## For AI agents
- DO: resolve-or-create the node device (`matterNode`) AFTER the per-endpoint
  creation loop, and never append its spec to `plan` — `multi`/`role_counts`
  must see endpoint specs only (or a single-endpoint node would flip to
  "multi" and rename its one real device with a role suffix it doesn't need),
  and the endpoint device must win the bare-name collision when a
  single-endpoint node's relay and its node device both want the same bare
  name (see the comment at the `_ensure_node_device` call site in
  `device_sync.create_devices`).
- DO: gate node-device creation on ep 0's own BasicInformation AttributeList
  (ADR-0003) — unknown is not yes, and a node mid-interview correctly gets no
  node device on that pass.
- DON'T: elect an application endpoint as a node's root. Endpoint 0 is always
  the root; there is no case where a different choice is defensible.
- DON'T: introduce a `<DeviceFactory>` for this or any future Matter device
  type in this plugin — it is a one-way door that removes every existing
  `<Device>` type from the New Device dialog.
- DON'T: rename or re-group any existing endpoint device in this PR's scope.
  Grouping (option B) and any name true-up are the next ADR-governed PR.
- DO: delete a node's non-`matterNode` devices before its `matterNode`
  device — `indigo.device.delete` refuses to delete the root of a non-empty
  group, which becomes load-bearing the moment the grouping PR lands.
