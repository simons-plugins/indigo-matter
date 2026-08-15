# Changelog

Notable changes per release. Versions are `YYYY.R.P`; the authoritative
current version is `Info.plist`'s `PluginVersion`.

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
