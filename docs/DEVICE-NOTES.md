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
