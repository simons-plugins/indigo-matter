# Device notes

Behaviours of specific Matter hardware that are not obvious from the spec, are
not reported by the device, and cost real time to rediscover. Everything here was
observed against physical units; where a claim is inferred rather than measured it
says so.

This is not a compatibility list. It is the set of things that surprised us.

> **Have a device that behaves differently?** Please open an issue — the
> plugin's automatic settable-attribute report gives you most of what a
> maintainer needs, and the **Matter → Explore Matter attributes (advanced)…**
> menu item dumps the rest.

## TP-Link Tapo (M-series plugs)

**Stock firmware exposes OnOff only.** The energy-measurement clusters
(ElectricalPowerMeasurement 0x0090 and ElectricalEnergyMeasurement 0x0091) arrive
only after a firmware update applied from the **Tapo app** — so a plug
commissioned straight into Indigo may never gain energy reporting until it has
been added to Tapo's own app at least once. Once updated, the existing Indigo
device picks up the new states automatically on the next reconcile.

**The two button holds do very different things, and one is destructive:**

| Hold | Effect |
|---|---|
| **~5 seconds** | Wi-Fi reset. **Keeps** existing Matter fabrics — the plug stays paired to Indigo and any other ecosystem. |
| **~10 seconds** | Factory reset. **Wipes every Matter fabric.** You will have to re-commission everywhere. |

Adding the plug to the Tapo app requires scanning its QR code; the app has no
manual pairing-code entry.

**Commissioning can fail on a "smart" Wi-Fi network.** A network with advanced
features enabled (band steering, client isolation, and similar) may fail
commissioning where a plain network succeeds. The plug must also be on the **same
subnet** as the Indigo server — mDNS discovery and link-local IPv6 do not cross
subnets.

## IKEA BILRESA (scroll wheel)

Three "channels", selected by the LED-row button on the front. The wheel routes
to a **different GenericSwitch endpoint trio per channel**:

| Channel | Endpoints (up / down / wheel-click) |
|---|---|
| 1 | 1 / 2 / 3 |
| 2 | 4 / 5 / 6 |
| 3 | 7 / 8 / 9 |

So one physical remote appears as nine Indigo button devices. The bottom
"system" button is pair/reset only and produces no Matter events.

The plugin maps events to `lastButtonEvent` (`shortPress`, `multiPress<N>`,
`longPress`) and increments `pressCount` on every event. **Wire triggers on
`pressCount` "any change" with a condition on `lastButtonEvent`** — not on
`lastButtonEvent` alone, which does not change when the same event repeats.
`multiPress<N>` carries the notch count, which is what makes proportional
scrolling possible.

## Aqara Light Switch H2 (and other momentary-only buttons)

The wired gang is an ordinary On/Off endpoint. Each **wireless** gang is a
separate GenericSwitch endpoint (4 and 5 on the vertical H2) whose FeatureMap is
**0x02 — MomentarySwitch and nothing else**: no MomentarySwitchRelease, no
LongPress, no MultiPress. Matter gates each Switch event on a feature (the
Events table's Conformance column, Matter 1.2 §1.12.6), and §1.12.7.3 is
explicit for this shape: such a switch "SHALL generate a single InitialPress
event for one interaction cycle" and "SHALL NOT generate any of the
ShortRelease, LongPress and LongRelease events". So it emits **`InitialPress`
and no other event, ever**.

The plugin used to discard `InitialPress` and wait for the terminal event that
tells short from long from multi — which never arrives on these, so the button
devices sat at their creation state forever and looked dead
([issue #231](https://github.com/simons-plugins/indigo-matter/issues/231)).
Since 2026.24.0 a momentary-only switch maps `InitialPress` to `shortPress` and
increments `pressCount`, like any other button. Devices created before that gain
the capability at the next reconcile — no need to delete and re-add them.

Such a switch cannot report a long or double press: the hardware never sends
one. `shortPress` is the only label these produce, so wire triggers on
`pressCount` "any change" as with every other Matter button.

One consequence to know about: if a momentary-only endpoint declares more than
two positions, `InitialPress` carries which position was pressed and the plugin
does not surface it — every position reads as `shortPress` on the one device.
(The release-capable path has always discarded the same field, but before this
release these switches produced nothing at all, so the ambiguity is newly
visible.) No such device has been reported; please open an issue if you have
one.

If a Matter button does nothing in Indigo, check the event log for a warning
that it "has not reported its Switch FeatureMap" — without that attribute the
plugin cannot tell a momentary-only switch from a release-capable one, and it
takes the safe option of ignoring the press.

## IKEA ALPSTUGA (air quality monitor)

Its OnOff cluster toggles the **display**, not power. The plugin currently treats
that as an actuator and creates a spurious relay device alongside the five
sensors — [issue #64](https://github.com/simons-plugins/indigo-matter/issues/64),
open. The intended fix is to decline OnOff as primary when its FeatureMap
declares DeadFront (0xFFFC bit 2); it is unimplemented for want of a second unit
to verify against.

## The current IKEA Matter range generally

Native **Matter over Thread**. A DIRIGERA hub is optional and only bridges the
older Zigbee line — the new devices do not need it. KLIPPBOK leak sensors present
as **contact sensors** (BooleanState), not a dedicated leak type.

## Sleepy Thread devices (ICDs)

Battery-powered Thread devices are Intermittently Connected Devices and only hear
you when they next poll their parent. A measured example polled at roughly **9.5
second** intervals.

Consequences worth internalising:

* A live attribute read takes **seconds, not milliseconds**. The plugin uses a
  30-second attribute timeout for exactly this reason, and dialogs never read
  live — they draw from subscribed states and cached limits.
* **A slow or absent answer is not evidence the device lacks an attribute.** Only
  its AttributeList (0xFFFB) can say that.
* A write that appears to time out may still have landed. The plugin therefore
  verifies by reading back rather than trusting either the acknowledgement or the
  timeout.

## Firmware-specific behaviour is normal

Two units of the same product on different firmware can expose different
attributes and behave differently — this is why the plugin's settable-attribute
report records the firmware version and re-reports when it changes. Some firmware
also **acknowledges a write and silently ignores it**, which is why every setting
write is confirmed by a subsequent read rather than by the ACK.

## Zigbee2MQTT-bridged CT bulbs (via the z2m Indigo plugin)

These apply to the **export** direction — an Indigo device driven by the z2m
plugin, bridged back out to Matter as an accessory.

**`whiteLevel` does not track `brightness`.** Measured live: `whiteLevel`
reporting `0.0` (and elsewhere, on other writes, a stale `20`) while the
lamp's real `brightness` was actually 29, then 49, then 69. A colour-
temperature write that sends the *stored* `whiteLevel` back to
`setColorLevels` is therefore not preserving the lamp's current level at
all — it publishes whatever stale number `whiteLevel` happens to hold, and on
this driver that number reaches the wire as a literal brightness, switching
an ON lamp OFF ([issue #281](https://github.com/simons-plugins/indigo-matter/issues/281)).
The original fix (2026-08-23) sourced a level from `brightness` instead and
sent it alongside every colour-temperature write, because `setColorLevels`
documents `whiteTemperature` as used *in combination with* `whiteLevel` and
the z2m plugin's colour handler at the time could not take a CT-only write
without reading a missing `whiteLevel` as 0 and switching the lamp off.

**CT writes are now temperature-only — no level co-write at all.** The z2m
plugin's colour handler (`plugin_color_control.py`, last modified
2026-08-26 — after the co-write shipped, at Simon's own request to its
author) now publishes `{"brightness": N}` and `{"color_temp": mired}` as two
independent MQTT messages, only when each key is present in the action, so a
CT-only write no longer risks switching the lamp off. The co-write itself
had by then turned actively harmful: on one lossy Tuya TS0502B the combined
write made the lamp echo its brightness back one point lower than commanded,
which the next CT write read and re-sent, ratcheting the level down roughly
one point per Apple adaptive-lighting tick while the lamp stayed lit —
measured 40→39→38→37→36→35 over an evening, three nights running.
`ColorTemperatureLightExport._set_color_temp` now sends only
`whiteTemperature`.

**This is a VERSION DEPENDENCY on the z2m plugin, not a one-time fact.**
Simon hand-re-applies local patches to the z2m Indigo plugin after
reinstalls. Rolling that plugin back to a build older than 2026-08-26
reopens the original #281 bug: a CT-only write will again risk a missing
`whiteLevel` reading as 0 and switching the lamp off. If CT-only writes
start switching lamps off again, check the z2m plugin's build date before
re-investigating this file.

**Warm-limit clamping is silent and permanent.** A bulb whose warmest
setting is 2500K accepts a commanded 2347K without complaint and simply
clamps its hardware to 2500K — there is no error, no rejected write, and no
attribute exposing the bulb's own physical limit for the plugin to read.
Reported back through Indigo, that clamp shows up as mireds 400 for a
commanded 426: a permanent ~26-mired gap between what was asked for and what
the lamp can do. Left unhandled, this is what looped Apple adaptive
lighting every ~3 seconds, forever (issue #281) — the plugin's fix (a
commanded-value push plus `CT_TOLERANCE_MIREDS`, ADR-0013) converges the
fabric on the commanded value and tolerates exactly this gap; it is not a
fix to the bulb's own reporting.

## Thread mesh (observed 2026-09-01)

**IKEA rev-2 vs rev-3 ThreadNetworkDiagnostics firmware differ in what they
report, not just in values.** Rev-2 firmware (ALPSTUGA, TIMMERFLOTTE) reports
FeatureMap 0 and a 22-entry AttributeList — the MLECNT-gated counters
(DetachedRoleCount, ChildRoleCount, RouterRoleCount, LeaderRoleCount,
AttachAttemptCount, PartitionIdChangeCount, ParentChangeCount) are absent
entirely, not zero. Rev-3 IKEA firmware (GRILLPLATS, BILRESA) reports
FeatureMap 15 and carries the full counter set. **The FeatureMap bit is not
the authority — the AttributeList is (ADR-0003):** Aqara's Presence
Multi-Sensor FP300 also reports FeatureMap 15, but its own AttributeList
omits 14–21 entirely, so it reports NONE of the counters despite the same
FeatureMap value the rev-3 IKEA devices carry. Always check the
AttributeList before reading a counter — do not assume it exists because the
cluster does, and do not assume it exists because FeatureMap says so either.

**None of the IKEA/Aqara nodes seen implements the provisional ExtAddress
(0x3F) / Rloc16 (0x40) attributes.** A router's own identity therefore has to
come from its route table's self entry — the row with NextHop 63, PathCost 0,
Allocated set, **and LinkEstablished clear** (a node is never its own radio
neighbour, so its self-entry is the one candidate WITHOUT an established
link) — rather than from those attributes directly. This check is gated to
Router/Leader roles only: a REED's RouteTable can otherwise contain a row
shaped exactly like a self-entry that actually describes a neighbouring
router, not itself. Verified against the 2026-09-01 fixture: 0x34 has two
RouteTable rows matching `NextHop==63, PathCost==0, Allocated` — its own
entry (router 62, LinkEstablished False) and its neighbour 0x3F's entry
(router 23, LinkEstablished True) — where only the LinkEstablished check
tells them apart; 0x40 (a REED) has a row shaped exactly like a self-entry
(router 23, `NextHop==63, PathCost==0, Allocated, LinkEstablished False`)
that actually describes 0x3F, which the role gate alone is what keeps 0x40
from being misread as router 23.

**Ext-address values arrive precision-lossy through matter-server's JSON**:
uint64 is serialised as a JS Number, so anything above 2^53 loses low bits.
Never use ext address as an identity key — key routers by RLOC16 (exact)
instead.

**matter-server's cache goes stale for sleepy devices.** Cached
PartitionId/LeaderRouterId values were hours old on two nodes and looked like
a partition split; a live read of the same nodes showed a single partition.
Treat a cached partition/leader disagreement as a staleness signal to
re-verify, not as evidence of an actual split.

## Alexa's Matter "turn on" for dimmable lights (#353)

**Observed:** saying "Alexa, turn on `<dimmable light>`" against an accessory
commissioned to Alexa (the Amazon fabric, vendor 4631) sends `onOff.on`, then,
tens of milliseconds later (40–85 ms observed), a SEPARATE plain `moveToLevel`
command — `level: 254` (the maximum), `transitionTime: 0` — rather than the
`WithOnOff` variant Apple Home was observed using for its own brightness
control (nothing shows Apple sending any level command for a plain turn-on).
Reproduced against both a Z-Wave dimmer and a Zigbee dimmer bridged from this
plugin, so it reads as Alexa behaviour independent of the radio; the fix
applies to all three light roles in code, but only `dimmableLight` was
live-tested — a colour-temperature or colour light is unconfirmed. Left
unhandled, that second command drove a dimmer Alexa turned on from off to
100%, overriding the dimmer's own on-level.

**Inference, not fact:** why Alexa sends that second command is not visible
to us. Upstream `t0bst4r/home-assistant-matter-hub` (issue #880) reports the
same shape and infers, from one A/B test there, that Alexa treats a light
advertising the Matter Lighting-feature minimum level while off as needing
an explicit level to look genuinely "on" — a non-minimum level advertised
while off made Alexa send `On` alone in that test. Our own live A/B
(2026-09-19, both arms from one Alexa controller on one build — see
ADR-0017's Confirmation) agrees with the BEHAVIOUR: minimum advertised while
off, `On` then `moveToLevel(254)`; a retained level advertised, `On` alone.
The MOTIVE is still upstream's inference — nothing we can observe says why.

**What we do about it:** rather than gate or drop the command — which also
carries "Alexa, set X to 70%" sent to an off light, the identical shape —
the bridge consumes a pushed `level: 0` but never lets it move `currentLevel`
to the Lighting minimum. There is no on/off test involved: it does not
matter whether the same push says the light is on or off, or what the
endpoint currently reports — a pushed `level: 0` simply never reaches
`currentLevel`. The attribute instead keeps the last non-zero level Indigo
confirmed. A plain `moveToLevel`, should one still arrive, is forwarded
completely unchanged — the fix does not neutralise that command. What
changes is that Alexa was observed not sending it in the first place once
the endpoint already advertises a non-minimum level: live, with a retained
level advertised while off, Alexa sent `on` alone (5 of 5). The retained
value is only what the bridge ADVERTISES while the light is off — it is
never sent to the device. With Alexa now sending a plain `On`, the level a
light comes on at is the dimmer's own decision (a configured on-level, or
restore-last-level) — which is how the reporter of #353 described Indigo's
older, non-Matter Alexa skill behaving; we have not independently tested
that skill ourselves. See
[ADR-0017](./adr/0017-currentlevel-while-off-is-the-last-confirmed-on-level.md)
and `BRIDGE_PROTOCOL.md` §4.2.

**Known limits a user might notice:**

- Any accessory whose stored `currentLevel` is still the Lighting minimum has
  no prior on-level to retain, so it still advertises the minimum — and can
  still be driven to 100% by this behaviour — until its first turn-on. That
  covers a brand-new export, and equally a light that has not been turned on
  since this fix was installed (or since it was first exported); after that
  first turn-on the exposure stops for that light.
- Recreating an accessory under a different role (for example, if a device's
  capabilities change and it is re-exported with a new role) resets a
  retained level back to the Lighting minimum while off, the same as a
  brand-new export — an ordinary restart does not do this, only a role
  change does.
- Migrating an exported accessory onto a different underlying device (a
  rekey — the same published accessory identity now driven by a different
  Indigo device) keeps the PREVIOUS device's retained level until the new
  device is itself turned on for real. This is deliberate, not a bug: it
  avoids re-exposing the newly-migrated accessory to the same first-turn-on
  override this fix exists to prevent, at the cost of showing a level for a
  moment that the new device never itself confirmed.
- A Matter scene recall to exactly the retained level, while the light is
  off, may not emit a level command at all (matter.js skips a level write
  that already matches `currentLevel`) — a pre-existing matter.js quirk,
  newly reachable now that "off" and "the retained level" coexist on the
  same attribute.
- Alexa's own threshold for what counts as "minimum" versus "a normal
  level" is not known. Live, the minimum drew the `moveToLevel(254)` and
  retained levels of 20% and 100% did not; nothing between the minimum and
  20% has been tried.
- While the light is off, a relative `step` command computes from the
  retained level rather than from the minimum. `move` is unaffected in
  practice: its target is ±∞, which the bridge clamps, so it always lands on
  0% or 100% regardless of what `currentLevel` starts from.
- If Indigo ever reports a dimmer ON at brightness 0, the bridge keeps the
  retained level rather than the minimum, and the plugin logs a warning in
  the Indigo Event Log, once per device, when that happens (#357).
