# Matter, Thread, and where Indigo + Domio fit

A plain-English guide to the Matter landscape and how the pieces of this
ecosystem — **Indigo**, the **indigo-matter plugin**, **matter-server**,
**Domio**, and **Apple Home** — fit together. If you want install steps, see
[INSTALL.md](./INSTALL.md); if you want the wire contract, see
[API.md](./API.md). This document is the "why does it work this way".

The plugin works in **both directions**, and most of this page is about the
first: Matter devices becoming Indigo devices. The second — selected Indigo
devices published outward as Matter accessories — has its own section,
[Indigo as a Matter bridge](#indigo-as-a-matter-bridge--the-other-direction).

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

**Thread devices work via the normal share-model flow** — the same
Apple Home → pairing code path as Wi-Fi. Validated with real hardware on
both transports: a Tapo P110M (Wi-Fi) and an Aqara FP300 presence
multi-sensor (Thread, via a HomePod border router).

Here's why. The one Thread operation the plugin's controller stack
([`matter-server`](https://github.com/matter-js/matterjs-server), matter.js,
Beta) can't do is *first-admin* commissioning — handing a factory-fresh
device the Thread network credentials over BLE. In the share model that step
is always the admin-1 ecosystem's job: Apple Home (or Alexa, or Google Home —
see below) provisions the device onto **its own** Thread mesh using its own
credentials and radios. From then on the device is just an IP endpoint — that
ecosystem's border router routes IPv6 between the Thread mesh and the LAN and
proxies the device's mDNS records, so when the plugin joins as a second admin
over IP it neither knows nor cares that the last hop is Thread.

Practical caveats: matter-server is Beta, and battery Thread devices are
"sleepy" (they wake on long intervals), which is where a beta controller is
most likely to be flaky — though the first validated Thread device was exactly
such a sleepy battery sensor and behaved (live unprompted attribute reports). Thread devices *bridged* into Matter by a
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

This plugin sidesteps the problem entirely with the **share model** — the
plugin's founding architecture decision:

> An ecosystem you already own — Apple Home, or equally Alexa / Google Home —
> commissions the device first: it owns the BLE step and gets the device onto
> your network. Then the device is *shared* to Indigo over plain IP. Indigo
> never needs BLE, Thread credentials, or proximity to the device.

That's only possible because of Matter's best feature: multi-admin.

## Fabrics and multi-admin: one device, many controllers

A Matter **fabric** is a controller's trust domain — a set of cryptographic
credentials a controller installs on a device. The crucial design choice in
Matter is that **a device can belong to several fabrics at once**. The spec
floor is five — and since each fabric slot costs the device persistent storage,
the floor is also what most devices ship. Treat five as your planning number. Each controller talks to the device directly and locally; none of them
knows or cares about the others.

A single plug in this house happily serves four admins simultaneously: Apple
Home, **Indigo (this plugin's fabric)**, the vendor's app, and Home Assistant
— with no cross-interference. Removing one fabric (e.g. "Remove from Indigo")
leaves the others untouched; only a factory reset wipes them all.

The plugin's fabric lives inside matter-server's storage directory. It is the
single point of total loss — if it's destroyed, every device must be
re-commissioned — which is why the plugin ships fabric **backup and restore**
(menu items: *Back up the Matter fabric…* / *Restore a fabric backup…*).

> **Why Apple Home calls our fabric "Matter Test" — this is expected.** When the
> plugin joins a device, Apple Home shows a "joined a new network" notification
> and lists the Indigo fabric under a name like **"Matter Test"** rather than
> "Indigo". Nothing is wrong. The fabric *label* is correctly set to "Indigo"
> (the plugin sets it, and it's what the device stores), but in that notification
> Apple displays the fabric's **Vendor ID**, not its label. matter-server (matter.js)
> commissions under the Matter **test vendor ID `0xFFF1`**, whose registered name
> is "Matter Test". Showing a custom name there would require a CSA-allocated
> Vendor ID — paid CSA membership plus product certification — the same limitation
> every non-certified Matter controller has (Home Assistant shows up the same way).
> It is purely cosmetic: control, security, and which fabrics the device belongs to
> are all unaffected.

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
3. **Enter the code**, in whichever of the two is closer to hand:
   - **Domio** → add Matter device → code, name, room. Domio POSTs it to the plugin.
   - **Indigo** → Plugins ▸ Matter ▸ *Commission device by setup code…* → the same
     code, name and room.

   Either way the plugin joins as a second admin over IP and creates the Indigo
   device(s). Discovery can take a couple of minutes on a busy LAN — the plugin
   waits up to 5.
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

**The two entry points are equals**, and for the same reason: once the code
exists, the rest is plain IP. The plugin menu's *Commission device by setup
code…* does exactly what Domio does — paste the pairing-mode code, Wi-Fi or
Thread alike. Neither is a fallback for the other, and Domio is not required.

Pick on convenience, not capability. Domio wins when you're standing at the new
device with the Home app open and the code has just appeared — it's the next tap
on the same phone, and you never go near the Mac. The plugin menu wins when
you're already in Indigo, or have the code some other way.

And if a device is already on your network and was never commissioned by anyone
(some vendor apps get devices onto Wi-Fi without Matter), its *printed* code
works directly in either entry point.

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

## Indigo as a Matter bridge — the other direction

Everything up to here is Indigo as a Matter **controller**: other people's Matter
devices becoming Indigo devices. The plugin also runs the reverse. A set of
Indigo devices you choose — Z-Wave, Insteon, Zigbee behind a plugin, MQTT,
anything with an Indigo device record — can be **exported**: published outward as
Matter accessories, so Apple Home sees and controls them like any other Matter
kit. Locally, with no cloud relay, and with no per-ecosystem bridge plugin.

If you have run Homebridge, this is the same idea over Matter's protocol instead
of HomeKit's — with the difference that Matter is one implementation reaching
every ecosystem that speaks it, rather than one bridge per ecosystem.

The bridge is a second npm package, `indigo-matter-bridge`, published on the
npm registry (0.5.0 onward) and exact-pinned by the plugin, so the
**Install/update the Matter bridge** menu item resolves it.
Setup steps are in [INSTALL.md](./INSTALL.md); this
section is the shape of it.

The two questions this page gets asked most are "can I export my lock?" and "why
isn't my *X* in the list?", so those tables come first; the reasoning follows
them.

### What can be exported (v1)

Indigo does not record what a device *is*. A relay may be a lamp, a plug, a lock,
a valve, a fan or a garage door, and nothing in the device model tells them
apart. So export asks you to **declare a role** per device, and defaults to the
safest reading rather than guessing. Only roles a device can legitimately take
are offered.

| Indigo device | Roles offered | Appears in ecosystems as |
| --- | --- | --- |
| Relay | **Plug** *(default)*, Light, Lock | On/Off Plug-in Unit · On/Off Light · Door Lock |
| Dimmer | **Dimmable light** *(default)*, Window covering | Dimmable Light · Window Covering |
| Dimmer, colour-temperature capable | **Colour-temperature light** *(default)*, Dimmable light, Window covering | Color Temperature Light |
| Dimmer, full colour | **Full-colour light** *(default)*, Dimmable light, Colour-temperature light, Window covering | Extended Color Light |
| Sensor, on/off | **Occupancy** *(default)*, Contact | Occupancy Sensor · Contact Sensor |
| Sensor, numeric | Temperature, Humidity, Light (lux), Pressure, Flow — the default is guessed from the device's units, and you can correct it | the matching Matter sensor type |
| Thermostat | Thermostat | Thermostat (setpoints and modes; no fan in v1) |

Two notes on that table. **Window covering** is offered for any dimmer because
Indigo represents blinds as dimmers; Matter's convention is 100 % = fully open,
and there is a per-export tick-box if your device runs the other way. And the
**numeric sensor** guess reads whatever unit hints the device carries (its
plugin's properties, its displayed value, then its name) — it is only a default,
and all five roles stay selectable.

**Locks export, and they do not auto-confirm anything.** A lock or unlock from an
ecosystem is passed to Indigo and nothing else: no optimistic state, no
synthesised confirmation. What the ecosystem shows moves only when Indigo's own
state moves, because the bolt is the authority.

### What cannot be exported, and why

Excluded devices still **appear in the picker with their reason** — they are
never silently missing, so you are never left hunting for a device that will
never show up. (The picker shows up to 300 matching devices at a time; past that
it says so and asks you to narrow the name filter.)

| Not exportable | Why |
| --- | --- |
| Anything this plugin created — including its own energy-meter devices | Loop guard: a Matter device is not re-exported over Matter. Checked on the owning plugin id, before the device's type is looked at, so it catches every one of the plugin's device types alike. These are filtered out of the picker entirely rather than shown with a reason. |
| **Valves** (as a relay role) | matter.js's valve cluster is an empty stub — the whole command surface would have to be written from scratch — and ecosystem support for the type is poor. v2 candidate. |
| **Fans** — both dimmer-backed and speed-control devices | Same reason: matter.js's fan cluster only seeds a default mode, so all fan behaviour would be ours to implement. v2 candidate. This is also why the exported thermostat has no fan control. |
| **Garage doors** (as a relay role) | Needs polarity data Indigo does not carry (`onState` true meaning *closed*, "on" meaning *close*), and getting it backwards is a physical-safety problem, not a cosmetic one. Blocked on the device-catalog work. |
| **Sprinkler** devices | Matter has no irrigation-controller type. Per-zone water valves would be a lossy fit, and water valves are descoped anyway. |
| **MultiIO** devices | No coherent way to represent one as a single accessory. |
| Sensors whose units aren't in the table above | No faithful Matter sensor type to map them to. |
| Sensors that report neither an on/off state nor a value | Nothing to publish. |
| Devices with no resolvable role | The device is not a relay, dimmer, sensor, thermostat, speed-control, sprinkler or MultiIO — typically another plugin's custom device class. There is nothing in the Indigo device model to map, so there is no role to offer. |

Beyond the device types, four v1 limits worth knowing up front:

- **Units are taken on trust.** Indigo declares no units anywhere, so a reading
  is assumed to already be in the unit Matter wants (°C, %RH, lux, m³/h).
  A Fahrenheit thermostat will export a wrong-looking number, and there is no
  fix inside Indigo's device model today. **Barometric pressure is the one
  exception**: Indigo's convention is hPa, the plugin knows it, and it converts
  to Matter's kPa — so a barometer reading in hPa is right as it stands, and
  "correcting" it to kPa is what produces a value ten times too high.
- **Pressure and flow sensors are exported but Apple Home has no UI for those
  Matter types**, so expect them not to appear there.
- **Energy and power are not exported.** Matter's electrical-sensor type is
  ignored by Apple Home's UI, so it buys nothing in the ecosystem this is
  written for. v2 candidate.
- **Action groups, schedules, triggers and variables are not exported.** Only
  devices.

### Two processes, opposite jobs

```text
   Matter devices  ──▶  matter-server  ──▶ ┐
   (in the house)        (controller,      │
                          Indigo's fabric) │
                                           ├──▶  indigo-matter plugin  ──▶  Indigo
   Apple Home  ◀──  bridge node  ◀─────────┘        (Python)
   (and any other  (device role,
    ecosystem)      matter.js)
```

There are now **two** Node processes, and they share a Node runtime and nothing
else. `matter-server` is the controller: it holds Indigo's own fabric and
commissions other people's devices. The **bridge node** is a Matter *device* —
it holds no fabric of its own, and instead gets commissioned *into* other
ecosystems' fabrics, exactly as a Matter plug does. Separate process, separate
storage, separate launchd job, separate port. One can be broken while the other
works, and an export failure never touches the devices Indigo controls.

**Why a separate process at all**, rather than the plugin doing Matter itself:
Indigo plugins are Python and the Matter library the plugin uses (matter.js) is
TypeScript. Keeping the whole Matter stack behind a process boundary is what lets
the plugin stay plain Python and speak Indigo devices, and it is the same
discipline the controller side already follows. The two halves talk over a
loopback WebSocket that nothing outside this Mac can reach, and the protocol is
versioned: a bridge node speaking a different **protocol** version is refused
rather than guessed at. That version is bumped only when the wire contract
itself changes, so most plugin upgrades need no new bridge node — but when it
does change, an old node left running across an upgrade is halted with a reason
rather than talked to on a guess.

Both halves present the bridge as one accessory containing an **aggregator** with
one child endpoint per exported device — the standard Matter bridge topology, and
the same shape the plugin already *consumes* inbound from Aqara, Hue and
SwitchBot hubs. One bridge, one pairing operation, one uncertified prompt, however
many devices you export.

### Nothing is exported until you say so

Export is **opt-in per device, default empty**, and that is a deliberate policy
rather than a UI convenience.

Exporting a device is *publishing* it. It becomes visible and operable from every
ecosystem the bridge is paired with, and from those ecosystems' accounts —
which means, for a voice assistant, from outside the house. Locks, valves,
garage doors and alarm-adjacent devices make an export-everything default
indefensible, so there isn't one: a fresh install exports nothing, pairs nothing
and runs no bridge process at all.

The consequence worth planning around: **there is no per-ecosystem export set in
v1**. There is one bridge and one allow-list, so a device you export is exported
to *every* ecosystem the bridge is paired with. If you want a device in one place
and not another, do not export it. (Per-ecosystem sets, and multiple bridges,
are a v2 idea.)

Devices this plugin created itself can never be exported — they are filtered out
of the picker entirely, so a Matter device cannot be re-exported over Matter into
a loop.

### Fabrics, again — and Apple takes two slots

The same multi-admin machinery described above works in this direction too. Each
ecosystem that pairs the bridge installs its credentials as a **fabric** on it,
and the bridge serves them all at once and independently. There is no small
fabric limit here of the kind physical devices have: the bridge is software and
its ceiling is far above any realistic number of ecosystems.

One thing to expect: **an Apple Home pairing creates two fabrics**, not one —
*Apple Home* and *Apple Keychain* (iCloud Keychain sync). Both show up in the
plugin's readouts and in the unpair picker. Neither is a stray, and removing
either is not a tidy-up.

Unpairing the **last** ecosystem resets the bridge completely: Matter's rules
make the node factory-reset itself when its fabric set empties, so it goes back
to advertising for commissioning. That is correct behaviour, and it is the same
outcome as *Reset Matter Bridge Pairings…* — worth knowing before you remove
what turns out to be the only one.

### Accessory identity, and why it must never move

**Back up
`~/Library/Application Support/com.simons-plugins.indigo-matter/bridge-node/`**,
and read the recovery notes in [INSTALL.md](./INSTALL.md) before using either of
the two destructive repair actions. That is the whole practical instruction; the
rest of this subsection is why it matters more than it looks.

Every exported device gets a stable identity derived from its immutable Indigo
device ID, and a Matter endpoint number allocated once against that identity —
never from its position in a list. Ecosystems remember both. The name you gave an accessory in Apple Home,
the room you put it in, the scenes and automations you built on it: all of that
is keyed to the identity, not to the accessory's name.

So the bridge treats its storage directory as sacred, and keeps an independent
record of which number belongs to which device purely so it can **notice** if
they ever disagree. If they do, it says so loudly and **does not repair it** — an
automatic repair would bless whatever went wrong and make the next occurrence
invisible too. And if that record is unreadable on a bridge that has been paired,
it refuses to export anything at all rather than quietly renumbering — silent
renumbering is how every accessory ends up duplicated in every ecosystem, with
the originals dead and removable only by hand. (Before the bridge has ever been
paired there is nothing to protect, so it just carries on.)

There is one everyday case that changes identity on purpose: **changing an
export's role**. Matter does not allow an endpoint to change device type, so the
accessory is removed and re-added, and every ecosystem treats it as brand new —
losing the name and room it had. The dialog warns you at the time.

### Which ecosystems this actually works with

Honestly stated, because the difference matters:

- **Apple Home** is the ecosystem this bridge is designed for, documented for,
  and the only one anything is claimed about. It is also the strictest
  attestation experience we can test — the uncertified prompt.
- **Alexa, Google Home and SmartThings are untested and unclaimed.** Not
  supported, not unsupported: nobody here has the hardware, no decision hangs on
  the answer, and the only remedy for a refusal — a real CSA vendor ID — is out
  of scope for a free plugin. The bridge is an ordinary multi-admin Matter
  bridge and there is no reason in principle they should refuse it, but a reason
  in principle is not a test. Google Home is the most likely to be awkward: it
  has historically been strictest about test-range vendor IDs. If you pair any of
  them, please report it.
- **Any Matter controller you already own** counts too, including Home Assistant
  and Indigo's own controller — the bridge does not care who commissions it.

**What has and has not been proven on live hardware** is stated once, in the
project [README](../README.md#status), rather than restated here where the two
copies would drift apart. The short version: the bridge has been commissioned
into a real Apple Home and exported devices have been controlled both ways, but
several legs — pairing driven from the plugin's own menu, a second ecosystem, a
full reboot — are still outstanding. Treat the export half as new.

### Why every ecosystem calls it "uncertified"

Because it is, deliberately, and there is nothing the plugin can do about it.

Matter attestation runs one way: the **commissioner checks the device's**
certificate. Inbound, the plugin is the commissioner, so it has a setting for
this (*Allow test/development device certificates*). Outbound the roles are
swapped — the bridge is the device and Apple is the commissioner — so the trust
policy belongs to Apple and there is no flag we could ship for someone else's
ecosystem, ever.

The bridge advertises with the specification's **test vendor ID** (`0xFFF1`), the
same posture Homebridge, matterbridge and Home Assistant's bridge all ship with.
Certification means CSA membership plus per-product testing, which is not
proportionate for a free community plugin. So: expect the warning, choose "Add
Anyway", and know that it says nothing about whether the bridge works.

(That test vendor ID is also why Apple Home shows the *controller* fabric as
"Matter Test", described earlier. Same number, two entirely different uses — one
is our controller's fabric identity, the other is the bridge's attestation
identity — and neither is a claim about the other.)


## Firmware updates (and why they matter more than usual)

Matter firmware arrives by two routes: the device **vendor's app**, and the
ecosystems themselves — those that act as Matter OTA providers push updates
too (**Apple Home auto-updates Matter accessory firmware**; observed on this
house's Tapo plug). The vendor app typically gets releases first, so check
there when you're waiting on a feature. And vendors are still actively
*adding Matter features* via firmware. Real example from this house: a Tapo P110M energy plug shipped exposing only on/off
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
  Give it the full window before retrying. If those five minutes pass with no
  join, the event log says so definitively ("the reconcile window closed and
  the device never joined") — nothing is retried automatically, so re-check
  the device is in pairing mode and try again.
- **Device unreachable after a vendor-app setup or firmware update.** Normal
  for a minute or two while it reboots and re-announces; the plugin marks it
  unreachable and self-clears on return. Persistently dead usually means a
  factory reset wiped the fabric — re-commission.
- **Capability missing (no energy states, no colour, …).** Firmware (see
  above), then diagnostics to inspect advertised clusters.

**Backups.** Back up the fabric after commissioning anything you'd hate to
re-pair. The plugin keeps rotating zips and a restore is menu-driven and
reversible.

**Exporting devices** has its own failure modes, its own logs and its own
recovery actions — see the *export bridge* table in
[INSTALL.md → Troubleshooting](./INSTALL.md#the-export-bridge).

---

*Further reading:* [INSTALL.md](./INSTALL.md) (setup) ·
[API.md](./API.md) (Domio ↔ plugin contract) ·
[IMPLEMENTATION.md](./IMPLEMENTATION.md) (internals) ·
[Home Assistant's Matter docs](https://www.home-assistant.io/integrations/matter/)
(an excellent ecosystem-neutral primer that inspired this page).
