# Changelog

Notable changes per release. Versions are `YYYY.R.P`; the authoritative
current version is `Info.plist`'s `PluginVersion`.

## 2026.23.0 — export every sensor you actually own

The sensor half of the export bridge, widened. Five new roles, an on/off
default that reads the device's name instead of always guessing occupancy, and
— the big one — devices whose plugin keeps its reading in a state of its own
can now be exported at all. On the database this was developed against that
last change moves 229 devices out of "no resolvable Matter role", which was
the largest exclusion bucket in the product by a factor of ten.

### Added

- **Export devices whose reading lives in a plugin-named state** (issue #252).
  Alarm-panel zones are the case that prompted this: a plugin defining its
  devices as `custom` gives Indigo no on/off flag to read, so every zone —
  ordinary PIRs, door contacts, smoke and CO detectors — was listed as having
  no resolvable Matter role. The export dialog now asks **which state is the
  reading**, offering the boolean states the device actually publishes, with a
  tick-box for a state that is true when *nothing* is detected. Map the first
  zone of a panel and every other device of the same type pre-selects the same
  state, so the rest need only their role. If the plugin later renames or drops
  that state, the export stops publishing and says so in the log rather than
  reporting a fabricated "nothing detected". Only on/off states are mappable in
  this version; a device whose only readings are numbers says so in the picker.
  See ADR-0012.
- **Smoke alarm and carbon monoxide alarm export roles** (issue #179). Two
  roles, not one combined accessory: Matter gives both one device type and
  selects the sensing half by feature, while an Indigo sensor is a single
  on/off reading meaning a single thing — exporting a smoke-only sensor as
  both would leave it permanently reporting a CO reading nothing measured.
  Read-only, like every other exported sensor; Matter makes the self-test
  command optional and the plugin has nothing to run. Heat alarms are
  deliberately not offered a role — Matter's alarm type senses smoke and CO
  only, and an ecosystem automating on a "smoke alarm" that cannot detect
  smoke is worse than no export.
- **Water leak, freeze and rain export roles** (issue #236). An Indigo leak
  sensor exports as Matter's own Water Leak Detector, so Apple Home shows it
  as a leak sensor with leak alerts instead of forcing it out as an occupancy
  tile; freeze and rain sensors get the adjacent Matter types. All three are
  read-only, like every other exported sensor.

### Changed

- **The occupancy export role is now labelled "Occupancy sensor
  (PIR/motion)"** (issue #252). Matter 1.x has no motion device type —
  Occupancy Sensor is the standard's own representation of a PIR — so an
  Indigo motion sensor already exported correctly, but the picker offered no
  word a user searching for "motion" would recognise, and the absence read as
  "this sensor cannot be exported". Nothing about the exported accessory
  changes; this is the label, plus the ceiling written down in MATTER.md and
  the bridge field notes.
- **The default role for an on/off sensor is now read from its name** (issues
  #236/#252). A binary sensor publishes one boolean and no unit, so its name
  is the only evidence about what it detects — "Utility Leak Sensor" now
  defaults to Water leak, "Front Door" to Contact, "Study PIR" to Occupancy,
  and anything unrecognised still falls back to Occupancy as before. It
  remains a default only: all five binary roles stay selectable in the picker.
  This matters more than it used to, because changing a role after export
  costs the accessory's room in every paired ecosystem.

## 2026.21.0 — Re-adopt a Matter accessory…, and role changes cost the room on purpose

> **This release bumps the bridge protocol to v2 — Matter export stops until
> the paired bridge-node release is installed via
> *Plugins ▸ Matter ▸ Install/update the Matter bridge*.** Version skew fails
> closed by design: nothing is un-paired and nothing is renumbered, but no
> accessory is served until the node matches. Restarting the bridge agent does
> not help — launchd relaunches the same node.

### Added

- **`Plugin ▸ Re-adopt a Matter accessory…`** (issue #219) — hands a
  left-behind accessory identity to a different Indigo device. An accessory
  is left behind when the Indigo device that exported it is deleted, or when
  it stops being exported; the room, name, scenes and automations built on it
  in Apple Home and every other paired ecosystem survive untouched, because
  nothing is re-paired and nothing is renumbered. Only works when the
  replacement device can take the same role as the accessory it is
  inheriting — cross-role re-adopt is refused, since that is exactly the
  wedge this release's role-change fix exists to eliminate. **A role change
  is NOT recoverable this way**: its old accessory number is retired on
  purpose (see Changed, below), so those accessories are not offered. An
  accessory un-exported by a plugin older than 2026.16.2 has no role/name on
  record and cannot be re-adopted either; export the device normally
  instead.

### Changed

- **Changing an export's role now costs the room, on purpose** (issue #240).
  Previously an in-place role change sometimes left Apple Home stuck on
  "could not change settings" until the home hub was restarted, because a
  cached accessory structure changed under a Matter endpoint number a
  controller believed was stable. A role change now removes the old
  accessory from every ecosystem and adds a new one under a fresh number —
  one deliberate re-room every time, predictably, instead of an occasional
  wedge. `Manage Matter Exports…` states this before you confirm. If you are
  already stuck from an older version, the hub-restart recipe in
  `docs/MATTER.md` still applies as a one-time recovery.

## 2026.20.0 — colour-while-off forwards, like brightness

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Changed

- **Picking a colour or colour temperature on a lamp that is showing off now
  reaches Indigo** (issue #235) — the same rule brightness follows: the
  bridge sends, Indigo decides. On many RGB lamps a colour write lights the
  lamp at the new colour (the device driver's own behaviour); on lamps with a
  white channel a colour-temperature change is normally stored without
  turning the lamp on (an RGB-only lamp refuses it with a logged reason).
  Previously direct colour commands were dropped while the accessory was off
  (scene recalls always went through).

## 2026.19.0 — exported brightness and colour wait for Indigo too

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Changed

- **Exported dimmers and colour lights no longer move optimistically**
  (issue #143, the LevelControl/ColorControl half — the onOff half shipped
  in 2026.17.0). Dragging the brightness slider, or picking a new colour or
  colour temperature, in Apple Home/Google/Alexa sends the command to Indigo
  and the control springs back only once Indigo reports the real level or
  colour — the same rule the on/off tile, locks and covers already followed.
  Commands while the accessory is showing off forward too — brightness turns
  the light on to that level (Indigo's own semantics); colour originally
  stayed gated in this version and was aligned with brightness in 2026.20.0
  (issue #235), which is the behaviour that actually ships.
  **No accessory is re-created and no room assignment is lost by this
  release** — unlike 2026.18.0's battery migration, this is a behaviour
  change only: the exported accessory's identity, name and room stay exactly
  as they were.
- **CIE xy colour commands now reach Indigo too.** An ecosystem driving an
  exported colour light in its xy representation used to be silently
  ignored; it now reports the equivalent hue/saturation, converted through
  the cluster's own conversion utility for that direction.

### Fixed

- **`extendedColorLight` accessories gain the `RemainingTime` attribute
  their device type mandates** (conformance fix, HR-1) — attribute addition
  only, no accessory recreation.
- **A dimmer command carrying `WithOnOff` can no longer strand a running
  ecosystem "on for N minutes" countdown** — closes the remaining half of
  issue #201's ghost-timer defect: the coupling that used to move the on/off
  state directly, ahead of Indigo's confirmation, is now itself invocation-
  driven.

## 2026.18.0 — battery level on exported accessories

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Added

- **Battery level now appears on exported accessories that report one**
  (issue #220) — automatic, no export setting to change. A device's own
  `batteryLevel` state is the whole trigger: the plugin declares a
  `PowerSource` cluster the first time it sees one, and every ecosystem then
  shows the reading on its normal update cadence. A device that has never
  polled (Indigo's own `0` at device creation) does not trigger a false
  "battery critical" alarm. **Migration note:** on the first reconnect after
  this update, every already-exported battery-bearing device is re-created
  once — endpoint identity is preserved, but names/rooms assigned in paired
  ecosystems may need re-assigning.

## 2026.17.0 — exported switches, plugs and lights wait for Indigo

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Changed

- **Exported on/off accessories no longer move optimistically** (issue #143,
  onOff half). Tapping a tile in Apple Home/Google/Alexa sends the command to
  Indigo and the tile confirms only when Indigo reports the device's real
  state — the same rule locks and covers already followed. A command that
  matches the accessory's current state is now forwarded instead of silently
  dropped.

### Fixed

- **Turning a device off in Indigo now cancels a running ecosystem
  "on for N minutes" countdown** (issue #201) — previously the countdown
  fired late and switched off a device that was already off (or had been
  turned back on since).

## 2026.16.2 — export conformance guards and port-bind verification

### Fixed

- **A managed matter-server or bridge node that lost its port-bind race right
  after bootstrap is now detected and reported** (issue #187) — previously
  the bridge node was never checked for port conflicts at all.
- **Un-exporting a device no longer destroys the evidence a future re-adopt
  needs** (issue #219 groundwork) — the endpoint map keeps the role/label and
  marks the record orphaned instead.

### Changed

- Warnings added for the two Alexa-relevant configurations: a non-default
  Matter port, and export counts past the community-reported ~50-endpoint
  ceiling. The full Alexa conformance sweep (issue #222) is documented in
  `docs/MATTER.md`.

## 2026.16.1 — manual commission now logs what happened

### Fixed

- **A manual commission that failed via a matter-server protocol error or a
  commissioning error logged nothing at all** (issue #227) — the reason was
  stored on the job for a poller that doesn't exist on the menu path, so a
  failed commission was indistinguishable from a silently successful one.
  `_fail()` now logs an ERROR line naming the device and the reason the
  moment it happens.

### Changed

- **Manual commission now narrates what it's doing, in the Event Log**
  (issue #226) — the raw `manual commission → 202 {'jobId': …}`
  acknowledgment read like an error to anyone unfamiliar with the wire
  format, and success logged nothing tied to the commission at all. It now
  reads:
  - Accepted: `Commissioning "<name>" — this takes up to a minute; the
    result will be logged here.`
  - Already running: `A commission for this setup code is already
    running — its result will be logged here.`
  - Succeeded: `Commissioned "<name>" → node <id>, created N Indigo
    device(s).`
  - Failed: the #227 ERROR line above.
  - Timed out: the existing warning, now led by the device's name (job id
    kept in parens), and — for the "matter-server gave up but the device
    may still join" case — extended with the most common real-world
    causes (a pasted original QR code instead of a fresh share code,
    an expired share window, or the device not being IP-reachable from
    this Mac).

  The raw wire acknowledgment (202/409) is unchanged in substance but now
  logs at DEBUG instead of INFO.

## 2026.16.0 — share an Indigo-commissioned device with another ecosystem

### Added

- **"Share a Matter device with another ecosystem…"** menu item (issue
  #210) — opens a fresh commissioning window on a device Indigo already
  controls, so a second ecosystem (Apple Home, Alexa, Google, Home
  Assistant) can add it too without you finding the device's own pairing
  mode. The reverse of the share model this plugin already uses to join
  devices commissioned elsewhere. Codes go to the Event Log, same reason
  the export bridge's pairing codes do — Indigo dialogs have no dynamic
  labels. Warns (never blocks) when a device's fabric capacity is getting
  tight; an Apple Home pairing alone uses two slots.
- **"Share this device with another ecosystem"** device action on the
  Matter Node device type — the same feature, reachable from a control
  page, trigger, or action group.

## 2026.15.0 — every Matter node gets an Indigo device

**Read the upgrade notes below before installing: this release renames some
existing devices and adds one device per Matter node.**

Matter describes a device as a *node* made of *endpoints*, with endpoint 0 as
the node's own "read me first" endpoint. This plugin has always created one
Indigo device per endpoint and nothing for endpoint 0 — so everything the node
itself carries (its identity, firmware version, reachability, battery, and on
some hardware its energy metering) had nowhere to live. Every other Matter
controller — Home Assistant, SmartThings, Hubitat, openHAB, Apple Home, Alexa
— has a node-level object; this plugin was the outlier. It does now.

### Upgrade notes — what changes on an existing install

Nothing you must do, but three visible changes, on the first reconcile after
upgrading:

1. **A new device per Matter node.** Named after the family (an Aqara FP300
   called "Landing sensor" gets a "Landing sensor" node device beside its
   "Landing sensor - Motion" and the rest). It carries the node's name,
   firmware version, reachability and — where the hardware reports one —
   battery level.

2. **Some devices are renamed.** Only devices still wearing a name *this
   plugin generated* are touched; **a device you renamed yourself is never
   renamed**. In practice the ones that change are single-endpoint devices,
   which gain their role suffix so the family reads consistently:
   `Office Plug` becomes `Office Plug - Switch`, and the bare `Office Plug`
   becomes the node device. Devices named after the product where the family
   has a better name also true up (`ALPSTUGA air quality monitor - Switch` →
   `Bedroom air quality monitor - Switch`).

   **Triggers, schedules, action groups and control pages are unaffected** —
   Indigo binds those to a device's *id*, not its name. What a rename does
   break is anything that looks a device up **by name**: Python scripts using
   `indigo.devices["Office Plug"]`, and any external integration keyed on the
   name rather than the id. If you have those, check them after upgrading.

3. **A node's devices are grouped together** in the device list, and node
   devices are filed alongside their siblings.

   ⚠️ **New hazard that comes with grouping:** deleting a grouped device in
   Indigo's UI now offers to delete **every device in that family**, and if
   you confirm, the plugin recreates the endpoint devices with **new ids** —
   so anything bound to the old ids stops working, silently. To hide a device
   you do not want, move it to a folder you do not look at. To remove the
   hardware, **decommission the node** instead. See `docs/MATTER.md`.

Battery routing also becomes evidence-based (below). On hardware whose power
source declares which endpoints it actually powers, a device that previously
showed a battery level may stop being fed one — the plugin logs a one-time
line naming any device this affects, so it is discoverable rather than silent.

### Added

- **A `Matter Node` device per node** (issue #204, ADR-0008), created only
  where the node's own attribute list evidences it. Carries `nodeLabel`,
  `softwareVersion` (parsed since the first release and never displayed until
  now), `reachable`, and `batteryLevel` where the node reports one.
- **Node-level energy** (issue #204): energy clusters on endpoint 0 are no
  longer dropped. Where the node says which endpoint they measure, the reading
  joins that device; where it cannot be attributed, it lands on the node
  device.
- **Grouping** (ADR-0009): a node's devices are grouped, so the family shows
  together and Indigo reads battery from the group's leading device.
- **"Recreate Matter node devices…"** menu item — the way back from deleting a
  node device, which is otherwise honoured permanently.
- **"Report this node's settable attributes"** device action, and the last
  survey summary on the node device's Edit dialog.

### Changed

- **`PowerSource.EndpointList` is now the authority for battery routing**
  (issue #205). The spec has always said which endpoints a power source
  powers; the plugin used to reconstruct that with a heuristic in four
  separate places, and issue #82 was a bug in one of them. Firmware too old to
  report it keeps the old heuristic, per source.
- Devices recreated after a deletion take their family's name rather than the
  product name.
- Node-level settings (the localization clusters) remain unimplemented: no
  device on the validation fabric reports them, and shipping a settings screen
  no hardware could exercise is how issues #190 and #197 happened. Tracked as
  issue #212.

### Fixed

- Battery readings can no longer cross-contaminate between the children of a
  bridge (the conditions for issue #82 are gone).
- A decommission no longer leaves a node unable to get a node device if it is
  later recommissioned onto the same node id.
- Meter links survive the empty attribute snapshot that follows a
  matter-server restart, so a node device cannot latch a duplicate power
  reading.
