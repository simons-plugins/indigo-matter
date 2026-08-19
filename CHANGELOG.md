# Changelog

Notable changes per release. Versions are `YYYY.R.P`; the authoritative
current version is `Info.plist`'s `PluginVersion`.

## 2026.24.1 — fans turn on

- **Turn On now works on fans that refuse the "On" mode** (issue #46). The
  plugin asked for a fan mode called "On" — which Matter deprecated in version
  1.2 and which appears in none of the speed lists a fan can declare. Older fans
  quietly treated it as "High"; newer ones reject it outright, and on those
  Indigo's Turn On did nothing at all. It now sets the speed to 100% instead,
  which every Matter fan accepts whatever modes it offers. Turn Off is unchanged
  — "Off" is the one mode every fan has.

  Setting a fan's brightness already worked and is untouched. Turn On means full
  speed rather than the speed it was last on; if you want a particular speed,
  set the brightness.
- **"Fan Always On" on a thermostat had the same problem**, and is fixed the
  same way — it now asks for "High", which every fan offers, instead of the
  deprecated "On".

  Both are **untested against real hardware** — there is no Matter fan on the
  development fabric — so if you own one, please report whether Turn On and Fan
  Always On behave.

## 2026.24.0 — momentary-only buttons work

- **A button that only reports "pressed" now reports it** (issue #231, reported
  by @coolcaper777 on an Aqara Light Switch H2). Its wired gang worked; both
  *wireless* gangs did nothing at all. Their Switch endpoints declare FeatureMap
  `0x02` — MomentarySwitch and nothing else — and Matter gates every Switch
  event on its feature, so those gangs emit `InitialPress` and no other event,
  ever. The plugin discarded `InitialPress` and waited for the release/long/
  multi-press event that tells one kind of press from another, which for these
  devices never comes: the Indigo device sat at its creation state for the life
  of the install. A switch declaring no release, long-press or multi-press
  feature now maps `InitialPress` to `shortPress` and increments `pressCount`,
  like any other button. Switches that *do* send a terminal event are unchanged
  — the leading edge is still discarded there, so a press is still counted once.
- **Existing button devices are healed at the next reconcile.** The capability
  is read from the endpoint's own FeatureMap and carried on the device; buttons
  created before this release pick it up automatically, with no need to delete
  and re-add them. If the FeatureMap later *changes* — a firmware update adding
  long-press to a gang, say — the device is corrected rather than left on the
  old answer.
- **A button whose device never reports its FeatureMap now says so.** Without
  that attribute the plugin cannot tell a momentary-only switch from a
  release-capable one, so it ignores the press — which looks exactly like the
  bug above. There is now one warning per such button in the event log, naming
  the device and what to send in if you hit it.

## 2026.23.10 — a pinned node is checked before it is trusted

- **A stale "Node bin directory" pin no longer shadows a newer node** (issue
  #101). The pin was honoured on an `npx`-exists check alone, and a successful
  install re-wrote it — so once a pin existed it kept itself alive. A user whose
  pin pointed at a leftover `/usr/local/bin/node` (an old Node.js `.pkg`, or
  Intel Homebrew on an Apple-Silicon Mac) could install a current node to fix
  the problem and have nothing change, because the pin still won; the only
  symptom was `matter-server connection lost … Connect call failed
  ('127.0.0.1', 5580)`. The pin is now checked before it is used: if the `node`
  beside it will not run, or reports a version below the required 22.13, the
  plugin says so — naming the pin, its version, and the node it was hiding —
  and falls back to auto-detect. Nothing is written to your prefs; the next
  successful *Install/update matter-server* re-pins to the node that ran it. A
  healthy pin (an nvm version directory, say) behaves exactly as before.
- **A rejected pin can no longer resolve straight back to itself.** The pin is
  usually one of the directories auto-detect checks first (that is where the
  install put it), so rejecting it used to walk into the same directory one step
  later — logging that the bad node had been ignored while going on to use it,
  and never reaching a healthy node further down the chain. When there genuinely
  is no alternative the pin is kept and the message says so, rather than
  claiming a fallback that did not happen.
- **The rejection message carries the real error.** "Bad CPU type in executable"
  or "Library not loaded: …" is the answer; it now appears in the log line
  instead of a flat "could not be run".
- The version check is bounded by a timeout, so a node on a stale network mount
  or a sleeping external disk cannot hang plugin startup.

## 2026.23.9 — the re-adopt picker leads with the likely answer

- **The re-adopt device picker now sorts its strongest candidates to the top**
  (issue #247). Selectable rows were alphabetical, which is scannable but says
  nothing about *which* row is the one you came for — on a large install the
  right device sat several screens down a list of every plug in the house. Two
  pieces of evidence already known to the dialog now rank it: a device whose
  name is the left-behind accessory's name (the same rule the bridge node's own
  "you could re-adopt this" nudge fires on), and a device that is already
  exported (`●`). Name match first, so the two together — the exact case the
  nudge reports — lead the list, then a name match, then any other exported
  device, then the alphabet as before. Nothing is hidden or dropped: selectable
  rows still come first and are still never truncated.

## 2026.23.8 — renaming, and what it does not do

Documentation only; no behaviour change.

- **Renaming an exported device in Indigo does not rename it in your
  ecosystem** (issue #221), and now both the Matter guide and the bridge field
  notes say so. Measured on 2026-08-19: the plugin *does* push the new name and
  the bridge records it within seconds — but Apple Home stores an accessory's
  name in your home, set when it was added, and a later change from the bridge
  does not override it. The Indigo name, and the optional *Name in ecosystems*
  field, decide what an accessory is called **when it first appears**, and
  nothing after that. To rename one you already have, rename it there.

## 2026.23.7 — the reboot leg is validated

Documentation only; no behaviour change.

The last outstanding leg of the export half's live validation is done. On
2026-08-19 the reference server was fully rebooted with **50 exported
accessories** paired into Apple Home. All 50 came back on the **same endpoint
numbers** under the same published identities; the bridge did not
re-commission itself (its install id and commissioning timestamp were
unchanged); both LaunchAgents started unattended; and the bridge was serving
**17 seconds** after the plugin started. Apple Home showed every accessory in
its own room with no duplicates and nothing unresponsive — including six that
had been *migrated*, and so publish an identity belonging to a different Indigo
device than the one driving them.

README, `MATTER.md` and `INSTALL.md` all carried the "still outstanding" note
and now record what was measured instead.

## 2026.23.6 — the documentation catches up with the sensors

Documentation only; no behaviour change.

### Changed

- **README** — the export section still said "the seven sensor types". There
  are now twelve, and they are named. It also gained the custom-state mapping,
  which had shipped without ever being mentioned to anyone reading the front
  page, and heat alarms among the roles deliberately not offered.
- **The Matter guide and the bridge field notes** — the mapping walkthrough
  still described a tick-box by its old name and its old meaning ("state is
  inverted", offered only for mapped devices). Rewritten for what the dialog
  does now: the role is suggested from the device's name, and *Reading is
  inverted* belongs to every binary sensor with contact sensors starting
  ticked. Both pages explain why that asymmetry exists — Indigo's on/off means
  *tripped*, Matter's contact means *closed* — and that a saved export is
  never re-derived, so a hand-corrected polarity stays corrected.
- **Heat alarms** are now in the "what cannot be exported" table rather than
  only in the prose above it.

## 2026.23.5 — contact sensors read the right way round

### Fixed

- **A contact sensor exported as itself reported every closed door as open.**
  Indigo's on/off state means *tripped* for every binary sensor — motion seen,
  leak seen, door OPEN. Matter agrees for all of them but one: its contact
  sensor reads true as **closed**. So exporting a Z-Wave door sensor straight
  through inverted it, and there was no way to correct it — the polarity
  tick-box only appeared for devices needing a state mapping, which an
  ordinary sensor does not.

  The tick-box now belongs to **every** sensor role that publishes a boolean,
  and a new contact export starts with it **ticked**, which is right for the
  common device. Change the role and it re-derives, because the same boolean
  means something different under a different role. Untick it and that is the
  last word — and an export you have already saved is never re-derived, so a
  polarity you corrected by hand stays corrected.

  **Existing exports are unchanged.** If you exported a contact sensor before
  this and its state reads backwards in your ecosystem, open it in
  *Manage Matter Exports…* and tick *Reading is inverted*.

## 2026.23.4 — the suggested role actually appears

### Fixed

- **Exporting a mapped device left the role blank, so every zone of a panel
  had to have its role set by hand.** The dialog pre-selects the state to read
  — from a sibling device of the same type where one exists — and the callback
  that suggests a role only fires when you *change* that menu. Having chosen
  for you, it never fired. The role is now suggested at the same moment the
  state is, and the status line says what it guessed and that you can change
  it. This is the whole point of the name heuristic, and on the path where it
  matters most it was doing nothing visible.
- **Zones named for more than one thing now read correctly.** "Patio Doors"
  and "Garden Doors" — ordinary door contacts — matched no needle and fell
  through to occupancy, because the needles were singular. Names of *things*
  are now matched in the plural too (doors, windows, gates, contacts,
  skylights); names of *phenomena* stay singular, since nobody calls a sensor
  "motions". The word-boundary anchors are unchanged, so "Drainage" still does
  not read as rain and "Defrost" still does not read as frost.

## 2026.23.3 — migrate onto a device you have already mapped

### Fixed

- **A device you had already exported with a state mapping was still refused
  as a migrate or re-adopt target**, telling you to do the thing you had just
  done. Both dialogs asked the classifier about the device *bare*, ignoring
  the mapping saved in its own export — so a Texecom zone exported five
  minutes earlier read back as one still needing a mapping. They now ask about
  the device **as configured**.
- **Migrating an accessory onto a mapped device would have left it dark.** The
  new entry took its options from the accessory's old device, so migrating
  from a Masquerade proxy — which needs no mapping, having a real on/off state
  — onto an alarm zone produced an entry with no mapping for a device that
  requires one: nothing would have been published, and the accessory would
  have gone quiet rather than moved. A state mapping describes *the hardware*,
  not the accessory riding on it, so it now comes from the device being
  migrated **to**. The mirror case is fixed with it: migrating off a mapped
  zone onto ordinary hardware no longer carries a state key the new device
  never publishes. Re-adopt already worked this way; migrate now agrees.

## 2026.23.2 — mappable devices come back to the migrate and re-adopt pickers

### Fixed

- **A device needing a state mapping vanished from "Migrate an exported
  accessory…" and "Re-adopt a Matter accessory…"** — no row, no reason, no
  clue (issue #252 follow-up, reported live). 2026.23.0 gave the classifier a
  third answer for devices Indigo does not type, and these two dialogs still
  asked their question the old way, so every such device — all 24 Texecom
  zones on the reference system — silently disappeared from both lists rather
  than being shown with a reason.

  They now appear, unselectable, saying *needs a state mapping first; export
  it directly, then retry*. That is the honest answer: inheriting an accessory
  never asks which state is the reading, so a migration onto an unmapped
  device would leave the accessory publishing nothing — dark rather than
  moved. Export the device directly first, which is where the mapping is
  asked, and it becomes an ordinary target afterwards. The same wording now
  appears if the migration is attempted anyway.
- **Both device pickers are now in alphabetical order.** They followed
  Indigo's own device order, which is not alphabetical — it collates
  punctuation Finder-style, so "Apple TV Power Socket" came before
  "AP_78:8a:20:b3:cc:4f" — and the list is built as two runs, selectable
  devices then explained ones. Across several hundred devices that reads as an
  alphabet restarting halfway down. Each block is now sorted A→Z on its own,
  so the devices you can pick still come first and never get truncated, but
  both halves are scannable.

## 2026.23.1 — the bridge node for the new sensor roles

The Node half of 2026.23.0's five new sensor roles — water leak, freeze, rain,
smoke and carbon monoxide. Pins `indigo-matter-bridge@0.13.0`; update the
plugin, then run *Plugins ▸ Matter ▸ Install/update the Matter bridge*.

Take the pair together. The older node does not know those roles, and refuses
an unknown role for the **whole** attach rather than the one endpoint — so
exporting a leak sensor against it would quieten every export, not just the
new one. Recovering is only this update: nothing is un-paired and no accessory
is renumbered.

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

## 2026.22.1 — the bridge node for migrate

The Node half of 2026.22.0. Install it with
*Plugins ▸ Matter ▸ Install/update the Matter bridge* — the plugin pins
`indigo-matter-bridge@0.12.0`, and the migrate action needs the rekey the node
release carries. Nothing else changed.

## 2026.22.0 — Migrate an exported accessory…

### Added

- **`Plugin ▸ Migrate an exported accessory…`** (issue #246) — moves a live
  export onto a **different Indigo device**, for the case that turns out to be
  the common one: a thing in your house getting replaced while both Indigo
  devices exist at once. A socket swapped for a new model, a light moved from
  Zigbee to Matter. Pick the export, pick the device that drives it now, and
  the accessory's room, name, scenes and automations survive **by
  construction** in every paired ecosystem — not restored afterwards, but never
  disturbed, because nothing is removed and re-added and no ecosystem ever sees
  a change. This is the difference between migrate and re-adopt: re-adopt
  rescues an accessory whose device is already gone, and by then Apple has
  purged the room within seconds, so it lands in the Default Room. Migrate,
  used before you delete the old device, keeps everything. The role must match
  — a migration cannot also change what the accessory is. See ADR-0011.

### Changed

- **Re-adopt's dialog, log and documentation stopped overclaiming.** They
  implied the room and name came back with the accessory; measured on live
  hardware, they do not — only the accessory number, absence of duplicates and
  correct control routing are restored. The wording now says so, and points at
  migrate for the case where the room really can be kept.

## 2026.21.1 — the bridge node for protocol v2

The Node half of 2026.21.0, pinned to `indigo-matter-bridge@0.11.0`. Exports
stay offline until it is installed via
*Plugins ▸ Matter ▸ Install/update the Matter bridge* — version skew fails
closed by design, and restarting the bridge agent does not help, because
launchd relaunches the same node.

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
