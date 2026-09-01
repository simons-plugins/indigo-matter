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
an ON lamp OFF. The export now sources the white level from `brightness`
itself for any lamp that is actually on ([issue #281](https://github.com/simons-plugins/indigo-matter/issues/281)).

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
entirely, not zero. Rev-3 firmware (GRILLPLATS, BILRESA, Aqara FP300) reports
FeatureMap 15 and carries the full counter set. Always check the
AttributeList before reading a counter — do not assume it exists because the
cluster does.

**None of the IKEA/Aqara nodes seen implements the provisional ExtAddress
(0x3F) / Rloc16 (0x40) attributes.** A router's own identity therefore has to
come from its route table's self entry — the row with NextHop 63, PathCost 0,
and Allocated set — rather than from those attributes directly.

**Ext-address values arrive precision-lossy through matter-server's JSON**:
uint64 is serialised as a JS Number, so anything above 2^53 loses low bits.
Never use ext address as an identity key — key routers by RLOC16 (exact)
instead.

**matter-server's cache goes stale for sleepy devices.** Cached
PartitionId/LeaderRouterId values were hours old on two nodes and looked like
a partition split; a live read of the same nodes showed a single partition.
Treat a cached partition/leader disagreement as a staleness signal to
re-verify, not as evidence of an actual split.
