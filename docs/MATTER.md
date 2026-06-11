# Matter, Thread, and where Indigo + Domio fit

A plain-English guide to the Matter landscape and how the pieces of this
ecosystem — **Indigo**, the **indigo-matter plugin**, **matter-server**,
**Domio**, and **Apple Home** — fit together. If you want install steps, see
[INSTALL.md](./INSTALL.md); if you want the wire contract, see
[API.md](./API.md). This document is the "why does it work this way".

---

## What is Matter?

Matter is a smart-home standard backed by Apple, Google, Amazon, Samsung and
hundreds of device makers under the Connectivity Standards Alliance. Its
promise: buy a device with the Matter logo and it works with every major
ecosystem, locally, without a vendor cloud.

The key thing to understand is that **Matter is not a radio**. It is a control
protocol that runs on top of ordinary IP networking. A Matter device talks
over one of:

- **Wi-Fi** — the device joins your normal wireless network.
- **Ethernet** — same, wired.
- **Thread** — a low-power mesh radio (more below).

Either way, control traffic is local IP (UDP, IPv6 link-local, discovered via
mDNS/Bonjour — the same multicast machinery as AirPlay and HomeKit). No cloud
round-trips, no vendor app needed for control.

## What does Thread have to do with Matter?

Thread is one of the *transports* Matter can run over. It's a low-power,
self-healing mesh radio (802.15.4, the same silicon family as Zigbee) designed
for battery devices — sensors, buttons, locks — that can't afford Wi-Fi's
power budget.

Thread devices can't talk IP to your LAN directly; they need a **Thread
Border Router (TBR)** to bridge the mesh onto your network. You probably
already own one — by 2026 almost every ecosystem's hub is a TBR:

- **Apple:** HomePod mini, HomePod (2nd gen), recent Apple TV 4K models.
- **Amazon:** Echo (4th gen), Echo Hub, Echo Studio, Echo Show 8/10/15/21
  (recent gens), and the eero router family.
- **Google:** Nest Hub (2nd gen), Nest Hub Max, Nest Wifi Pro.

Two distinctions that save a lot of confusion:

- **Thread ≠ Matter.** Thread is plumbing; Matter is the language. Some Thread
  devices speak HomeKit-over-Thread or proprietary protocols instead of Matter.
- **A Matter Wi-Fi device needs no border router at all.** It's just another
  client on your LAN.

### Thread status in this plugin

**Thread devices are expected to work via the normal share-model flow** —
the same Apple Home → pairing code → Domio path as Wi-Fi (validated with
Wi-Fi hardware; Thread validation pending a real device).

Here's why. The one Thread operation the plugin's controller stack
([`matter-server`](https://github.com/matter-js/matterjs-server), matter.js,
Alpha) can't do is *first-admin* commissioning — handing a factory-fresh
device the Thread network credentials over BLE. In the share model that step
is always the admin-1 ecosystem's job: Apple Home (or Alexa, or Google Home —
see below) provisions the device onto **its own** Thread mesh using its own
credentials and radios. From then on the device is just an IP endpoint — that
ecosystem's border router routes IPv6 between the Thread mesh and the LAN and
proxies the device's mDNS records, so when the plugin joins as a second admin
over IP it neither knows nor cares that the last hop is Thread.

Practical caveats while this remains unvalidated: matter-server is Alpha, and
battery Thread devices are "sleepy" (they wake on long intervals), which is
where an alpha controller is most likely to be flaky — a mains-powered Thread
device is the gentler first test. Thread devices *bridged* into Matter by a
hub you own (Aqara, Hue, SwitchBot) are a separate, hub-dependent route — see
Bridges below. And should a true gap surface, the WebSocket protocol the
plugin speaks is deliberately compatible with Home Assistant's
python-matter-server, so a different backend is a swap, not a rewrite.

## Bluetooth, commissioning, and why this plugin doesn't need either

When you pair a brand-new Matter device ("commissioning"), the very first step
usually happens over **Bluetooth LE**: the commissioner sends the device your
Wi-Fi credentials (or Thread network key) over BLE, the device joins the
network, and BLE is never used again.

This is why phones and ecosystem hubs make good *first* commissioners — they
have BLE radios and (for Thread) the network credentials. A headless Mac
running Indigo is a poor first commissioner: matter-server runs without BLE on
macOS, and macOS has no Thread credential store.

This plugin sidesteps the problem entirely with the **share model** (the
architecture decision recorded as ADR-0006, option "C4"):

> An ecosystem you already own — Apple Home, or equally Alexa / Google Home —
> commissions the device first: it owns the BLE step and gets the device onto
> your network. Then the device is *shared* to Indigo over plain IP. Indigo
> never needs BLE, Thread credentials, or proximity to the device.

That's only possible because of Matter's best feature: multi-admin.

## Fabrics and multi-admin: one device, many controllers

A Matter **fabric** is a controller's trust domain — a set of cryptographic
credentials a controller installs on a device. The crucial design choice in
Matter is that **a device can belong to several fabrics at once** (typically
5+). Each controller talks to the device directly and locally; none of them
knows or cares about the others.

A single plug in this house happily serves four admins simultaneously: Apple
Home, **Indigo (this plugin's fabric)**, the vendor's app, and Home Assistant
— with no cross-interference. Removing one fabric (e.g. "Remove from Indigo")
leaves the others untouched; only a factory reset wipes them all.

The plugin's fabric lives inside matter-server's storage directory. It is the
single point of total loss — if it's destroyed, every device must be
re-commissioned — which is why the plugin ships fabric **backup and restore**
(menu items: *Export fabric backup…* / *Restore fabric backup…*).

## The pieces, end to end

```text
                       ┌── commissioning (one-off per device) ──┐

   Matter device ◀─BLE+Wi-Fi── Apple Home (admin 1, iPhone/HomePod/Apple TV)
        ▲                              │
        │                              │ "Turn On Pairing Mode" → setup code
        │                              ▼
        │                      Domio iOS app ──HTTPS (API.md)──▶ indigo-matter plugin
        │                                                              │
        └────── IP (admin 2: the Indigo fabric) ◀── matter-server ◀────┘ WebSocket
                                                                       │
                       ┌── runtime control (for the device's life) ────┘
                       ▼
        Indigo devices: triggers, schedules, action groups,
        control pages, HTTP/WebSocket API, Domio control
```

| Piece | Role |
| --- | --- |
| **Apple Home** | First commissioner (admin 1). Does the BLE onboarding and, for Thread devices, provides the border router (HomePod/Apple TV). Alexa or Google Home + their TBRs can play this role instead — see "Don't have Apple Home?" below. |
| **Domio** (iOS app) | Owns the *add device* UX. Relays the pairing code from Apple Home to the plugin. One-shot per device — not in the control path afterwards. |
| **indigo-matter** (this plugin) | Holds the Indigo fabric, translates Matter clusters ↔ Indigo device types, serves the Domio HTTP API, supervises matter-server. |
| **matter-server** (Node.js) | The actual Matter controller stack (matter.js). Speaks Matter on the network; the plugin drives it over a local WebSocket. |
| **Indigo** | Where the devices live. A Matter plug is a relay, a Matter bulb is a dimmer — first-class citizens in every Indigo feature. |

## Adding a Matter device

The normal flow (Wi-Fi device):

1. **Commission in Apple Home first.** Scan the device's QR code in the Home
   app. If your main Wi-Fi uses "advanced" features (WPA3-only, 802.11r fast
   roaming, band steering), a basic 2.4 GHz IoT SSID is far more reliable for
   onboarding — but it **must be the same subnet** as the Indigo Mac (see
   Troubleshooting).
2. **Open pairing mode.** In the Home app: device → settings →
   **Turn On Pairing Mode**. Apple Home shows a fresh setup code (the printed
   QR code will *not* work for this — pairing mode generates a new one-time
   code).
3. **Add in Domio.** Domio → add Matter device → enter the code (plus a name
   and room). Domio POSTs it to the plugin; the plugin joins as a second
   admin over IP and creates the Indigo device(s). Discovery can take a
   couple of minutes on a busy LAN — the plugin waits up to 5.
4. Done. The device appears in Indigo (the room becomes a device folder) and
   is controllable from everything Indigo offers, plus Domio.

**A point worth being explicit about:** once the pairing-mode code exists, the
join itself is **pure IP** — no Bluetooth, no phone, no proximity to the
device. Opening pairing mode makes the device re-announce itself over mDNS on
the network it's already on; the plugin then does PASE/CASE over IP. This is
equally true for **Thread** devices (the join rides the border router exactly
like control traffic does). The phone is only needed for the two things that
genuinely require it: the out-of-box BLE commissioning, and tapping "Turn On
Pairing Mode". Everything downstream of the code is ecosystem-free.

**Without Domio** (advanced): because of the above, the plugin menu's
*Commission device by setup code…* is fully equivalent to the Domio flow —
paste the pairing-mode code by hand, Wi-Fi or Thread alike. Domio's value is
purely workflow: you're standing at the new device with the Home app open
when the code appears, and Domio is the next tap on the same phone — name,
room, progress, done, without going near the Mac. And if a device is already
on your network and was never commissioned by anyone (some vendor apps get
devices onto Wi-Fi without Matter), its *printed* code works directly in
either entry point.

**Removing:** plugin menu → *Decommission Matter device…* — this removes only
the Indigo fabric; the device stays in Apple Home and any other ecosystem.
(Deleting the Indigo device alone does *not* work — by design, the plugin
recreates it the next time it reconciles with matter-server, e.g. after a
plugin restart or reconnect. Only a factory reset on the device itself removes
everything.)

### Don't have Apple Home? Alexa or Google Home work too

The plugin and Domio never care *which* ecosystem is admin 1 — they just
consume a pairing code. Apple Home is the smoothest path for Domio users
(you're on an iPhone already), but any Matter ecosystem can play the role:

- **Alexa:** commission the device in the Alexa app, then device settings →
  **Other assistants and apps** → it generates a pairing code. For Thread
  devices a TBR-capable Echo (see the list above) plays the Apple TV's part.
- **Google Home:** commission in the Google Home app, then device settings →
  **Linked Matter apps & services** → share. Nest Hub/Wifi devices are the
  TBRs.

One rule to remember for **Thread** devices: the device joins the mesh of
whichever ecosystem commissions it, so that ecosystem's border router must
stay online for the device to be reachable — an Apple-commissioned device
rides the Apple TBR, an Alexa-commissioned one rides the Echo. (Pre-Thread-1.4,
different vendors' meshes in one house are separate networks.) For Wi-Fi
devices none of this applies — no TBR is involved at all.

## Sharing with other platforms (and vendor apps)

Multi-admin works in every direction. The same pairing-mode trick adds the
device to Google Home, Alexa, or Home Assistant alongside Indigo.

Adding the device to its **vendor app** is often worth doing once, because
firmware updates usually ship through it (see below). Caution with reset
buttons during vendor onboarding — e.g. on TP-Link Tapo plugs a ~5 s hold is
a Wi-Fi-only reset (Matter fabrics survive), while a ~10 s hold is a **factory
reset that wipes every fabric** and undoes all your commissioning.

### Bridges

A Matter **bridge** exposes non-Matter devices (Zigbee, proprietary RF) as
Matter endpoints — Aqara, SwitchBot, and Hue hubs all do this. The plugin
supports bridges: each bridged child appears as its own Indigo device. It's
also a practical route for Zigbee and other non-Matter sensors you already
own behind such a hub.

## What's supported (device classes)

The plugin maps Matter clusters to native Indigo device types:

| Matter capability | Indigo device |
| --- | --- |
| On/Off (plugs, switches, lights) | Relay |
| Dimming (Level Control) | Dimmer |
| Colour & colour-temperature | Colour dimmer (RGB + white-temp UI) |
| Sensors: temperature, humidity, occupancy, contact, illuminance, pressure, flow | Sensor (one device per measurement) |
| Thermostats (incl. attached fan) | Thermostat |
| Standalone fans | Speed-controlled dimmer |
| Window coverings (blinds, shades) | Dimmer (100% = open) |
| Door locks | Relay with lock UI |
| Valves (water/irrigation) | Relay (with flood-safe toggle behaviour) |
| Buttons / scene switches (Generic Switch) | Button device firing Indigo trigger events |
| Smoke / CO alarms | Sensor (alarm latch) |
| Air quality (AQI, CO₂, PM2.5, TVOC) | Sensors (one per metric) |
| Power & energy metering | `curEnergyLevel` (W) / `accumEnergyTotal` (kWh) states on the primary device |
| Battery level | `batteryLevel` on every device of the node |
| Bridges | One Indigo device per bridged child endpoint |

A node exposing several capabilities gets several Indigo devices (e.g. a
multi-sensor becomes one device per measurement); secondary capabilities like
energy metering and battery merge into the primary device's states.

## Firmware updates (and why they matter more than usual)

Matter devices typically receive firmware through their **vendor's app**, and
vendors are still actively *adding Matter features* via firmware. Real
example from this house: a Tapo P110M energy plug shipped exposing only on/off
over Matter — the energy-measurement clusters (a Matter 1.3 feature) appeared
only after a firmware update via the Tapo app. The plugin re-reads a node's
capabilities whenever it rejoins, so the existing Indigo device **gained its
energy states automatically** — no restart, no re-pairing, no recreation.

Rule of thumb: if a device seems to be missing a capability you know it has,
check for firmware first. The plugin's diagnostics endpoint (see API.md §3.5)
lists exactly which clusters a node is advertising, which settles the
"device or plugin?" question in one request.

## Troubleshooting

**Network first.** Matter assumes a flat residential network. The plugin, the
device, and matter-server must share **one subnet/VLAN**: discovery is mDNS
multicast and transport is link-local IPv6, and neither crosses routers.
You do *not* need "IPv6 from your ISP" — link-local addresses are
self-assigned and always present.

Common failure modes, in rough order of likelihood:

- **Commissioning fails in Apple Home on the main SSID.** Advanced Wi-Fi
  features (WPA3-mixed/PMF, 802.11r, band steering) break many 2.4 GHz-only
  IoT radios during onboarding. Use a backward-compatible IoT SSID — on the
  same subnet.
- **Apple Home controls it, Indigo can't reach it.** The IoT SSID is on a
  separate VLAN, or AP "client isolation" is blocking station-to-LAN traffic.
  (Apple Home can ride BLE/hubs; the plugin is pure IP, so it notices first.)
- **"1 discovered, attempt failed" or discovery timeouts.** Pairing-mode
  windows are short (~15 min) — regenerate the code and commission promptly.
  mDNS snooping/filtering on managed switches also causes this.
- **Commissioning seems to time out, then the device appears anyway.** LAN
  discovery can exceed 60 s; the plugin waits 300 s and reconciles late joins.
  Give it the full window before retrying.
- **Device unreachable after a vendor-app setup or firmware update.** Normal
  for a minute or two while it reboots and re-announces; the plugin marks it
  unreachable and self-clears on return. Persistently dead usually means a
  factory reset wiped the fabric — re-commission.
- **Capability missing (no energy states, no colour, …).** Firmware (see
  above), then diagnostics to inspect advertised clusters.

**Backups.** Export a fabric backup after commissioning anything you'd hate to
re-pair. The plugin keeps rotating zips and a restore is menu-driven and
reversible.

---

*Further reading:* [INSTALL.md](./INSTALL.md) (setup) ·
[API.md](./API.md) (Domio ↔ plugin contract) ·
[IMPLEMENTATION.md](./IMPLEMENTATION.md) (internals) ·
ADR-0006 in the Domio repo (the architecture decision behind the share model) ·
[Home Assistant's Matter docs](https://www.home-assistant.io/integrations/matter/)
(an excellent ecosystem-neutral primer that inspired this page).
