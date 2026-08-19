# indigo-matter

Matter device support for the [Indigo](https://www.indigodomo.com) home automation server —
**in both directions**.

**Matter in (the controller).** Add Matter devices from the **Domio** iOS app or the
plugin's own menu; control them as first-class Indigo devices (triggers, schedules, action
groups, control pages, API). The plugin manages a
[`matter-server`](https://github.com/matter-js/matterjs-server) instance, holds the
Indigo-owned Matter fabric, translates Matter clusters ↔ Indigo device types, and owns
runtime control for each device's lifetime.

**Matter out (the bridge).** Publish selected *Indigo* devices — Z-Wave, Insteon, Zigbee,
MQTT, anything with a device record — outward as Matter accessories, so Apple Home can see
and control them locally. Opt-in per device, nothing exported by default, from a second
managed process. Apple Home is validated end-to-end. Alexa pairs and controls too, with a
known caveat: newly-exported accessories can show stale/unresponsive in Alexa for some
minutes before converging (issue #143). Google Home and SmartThings remain untested and
unclaimed. See [Exporting Indigo devices](#exporting-indigo-devices-matter-out).

> **This is an independent, uncertified implementation.** It is not certified by the
> Connectivity Standards Alliance (CSA), and is not affiliated with or endorsed by them.
> It works, and is validated against real hardware — but it carries none of the
> interoperability guarantees that come with a certified Matter controller. See
> [Trademarks and certification](#trademarks-and-certification).

```text
Domio (iOS) ─┐
             ├── setup code ──▶ indigo-matter ──WebSocket──▶ matter-server ──IP──▶ Wi-Fi + Thread devices
plugin menu ─┘                       │
                                     └──WebSocket──▶ bridge node ──Matter──▶ Apple Home
                                        (export, opt-in)
```

## Adding devices

The two entry points are equivalent, for Wi-Fi and Thread alike. There are three ways a
device reaches one of them:

- **Share model** — the usual path. An ecosystem you already own (Apple Home, Alexa,
  Google Home) does the out-of-box Bluetooth onboarding: the one step a headless Mac
  can't, since matter-server runs without BLE. The device is then shared to Indigo over
  plain IP, and the plugin joins as a second admin.
- **Factory-fresh Thread, via Domio** — iOS commissions the device to the iPhone as part
  of that flow, which is what supplies the Thread credentials.
- **Already on your network, never commissioned** — its printed code works directly, from
  either entry point.

## Exporting Indigo devices (Matter out)

The plugin can also wear the Matter **device** role: a set of Indigo devices you choose is
published as accessories on a Matter bridge that Apple Home commissions like any other
accessory. One bridge, one pairing, however many devices.

- **Opt-in per device, default empty.** A fresh install exports nothing, pairs nothing and
  runs no bridge process. Exporting is publishing — the device becomes operable from every
  ecosystem the bridge is paired with — so it is always a deliberate act.
- **You declare what each device is.** Indigo cannot tell a plug from a lamp from a lock,
  so the export dialog asks, and suggests a role from the device's name. Relays, dimmers,
  colour and colour-temperature lights, window coverings, locks, thermostats and twelve
  sensor types — occupancy (PIR/motion), contact, water leak, freeze, rain, smoke, carbon
  monoxide, temperature, humidity, light, pressure and flow — are supported. Valves, fans,
  garage doors, sprinklers, heat alarms and MultiIO are not, and appear in the picker
  **with the reason**.
- **Devices whose plugin keeps the reading in a state of its own can be exported too.**
  Alarm-panel zones are the common case: every zone is a "custom" device with no on/off
  flag Indigo can read, so the dialog asks which of its states is the reading. Map the
  first zone of a panel and the rest pre-select the same state. Only on/off states can be
  mapped in this version.
- **Expect an "uncertified accessory" prompt** and choose *Add Anyway*. The bridge presents
  a development attestation certificate — as Homebridge, matterbridge and Home Assistant's
  bridge all do — and outbound attestation is the commissioning ecosystem's policy, not
  something this plugin can change.
- **Apple Home and Alexa validated; Google Home and SmartThings untested.** Apple Home
  works end-to-end. Alexa pairs and controls too, with a known caveat: newly-exported
  accessories can show stale/unresponsive in Alexa for some minutes before converging
  (issue #143). Google Home and SmartThings remain untested and unclaimed; nothing here
  says they will or won't work.
- **A second npm package** (`indigo-matter-bridge`), on the npm registry and installed
  from its own menu item.

Setup, pairing, recovery and troubleshooting:
[full install guide](https://simons-plugins.github.io/indigo-matter/INSTALL.html). How it
works and what it can represent:
[MATTER.md → Indigo as a Matter bridge](docs/MATTER.md#indigo-as-a-matter-bridge--the-other-direction),
or the typeset version:
[Field Notes № 3 → The bridge](https://simons-plugins.github.io/indigo-matter/bridge.html).

## Status

**Matter in — built and live-validated, including with real hardware.** The full device-class
catalogue is shipped — lighting (on/off/dimmer/colour), sensors, thermostats, fans,
window coverings, locks, valves, buttons, smoke/CO, air quality, energy metering,
battery levels, and bridges — plus failure hardening, fabric backup/restore, and a
managed launchd LaunchAgent for matter-server. A real Tapo energy plug has been
commissioned end-to-end through Domio and reports live power/energy to Indigo.

**Validated with both Wi-Fi and Thread devices**, and from both entry points — Domio and
the plugin's own commission-by-setup-code menu. Thread provisioning is done by a device
with a BLE radio and the network credentials (an admin-1 ecosystem, or the iPhone itself
during the Domio flow); the plugin then joins over IP via that ecosystem's border router
(HomePod/Apple TV, TBR-capable Echo, Nest Hub). See
[Field Notes № 2 — The controller](https://simons-plugins.github.io/indigo-matter/controller.html)
for how the pieces fit.

**Matter out — built, unit-tested on both sides, and validated live across every leg but
one.** The allow-list, the role mapping, endpoint persistence, pairing and fabric
management, and the managed LaunchAgent are all in place, and the bridge node's own suite
stands up a real Matter stack. On live hardware the bridge **has** been commissioned into
Apple Home (uncertified prompt and all), pairing driven from the plugin's own menu **has**
worked end to end, a second controller **has** joined alongside Apple Home — Alexa, as
fabric #3 — an on/off light and a dimmer **have** been controlled in both directions, the
one-click managed LaunchAgent chain **has** installed and run the bridge without hand
intervention, and accessory identity **has** survived an upgrade without duplicating
(2026-08-04 → 06, on the reference server), and accessory identity **has** survived a
full **reboot** of the host — 50 exported accessories back on the same endpoint numbers
under the same identities, no re-commissioning, both LaunchAgents up unattended and the
bridge serving 17 seconds after the plugin started, with Apple Home showing every one of
them in its own room and no duplicates (2026-08-19). Treat the export half as new. See
[MATTER.md](docs/MATTER.md#indigo-as-a-matter-bridge--the-other-direction) for how it
works.

## Setup

Install the plugin from the **Indigo Plugin Store** (or download the
`.indigoPlugin.zip` from [Releases](https://github.com/simons-plugins/indigo-matter/releases)
and double-click it), then set it up. It doesn't speak
Matter directly — it drives **matter-server**, a separate Node.js process holding the
Indigo-owned fabric. In local mode the plugin installs and manages that for you, so setup
from here is three steps and no Terminal beyond installing Node:

**1. Install Node.js 22** (≥ 22.13.0):

```bash
brew install node@22
brew link node@22
node --version          # confirm v22.x
```

Leave **Node bin directory** blank in the plugin config — auto-detect finds Homebrew
first. (nvm works too; pin `nodeBinDir` explicitly if you use it.)

**2. Install the Matter controller:** **Plugins ▸ Matter ▸ Install/update the Matter
controller (matter-server)**. This installs the pinned version with the *same* Node it
will run the server with, and pins
that Node — which avoids the most common failure, installing with one Node and running
with another whose native modules won't load.

**3. Verify** — the Indigo event log should show:

```text
connected to matter-server, listening
reconciled N Matter node(s)
```

`N` is 0 on a fresh install; that's fine. If you instead see
`Connect call failed ('127.0.0.1', 5580)`, matter-server isn't running — that, plus
the "On another computer" mode, fabric backups, upgrading and uninstalling, is covered in the
[full install guide](https://simons-plugins.github.io/indigo-matter/INSTALL.html).

To **export** Indigo devices as well, add a fourth step — **Plugins ▸ Matter ▸
Install/update the Matter bridge** — and then pick your devices in *Manage Matter
Exports…*. The bridge starts itself once something is exported. Full walkthrough in the
install guide.

## Requirements

- Indigo 2025.2+ on macOS.
- **Node.js ≥ 22.13.0** (Homebrew is the paved road; nvm works too) and the `matter-server`
  npm package — Beta, **exact-pinned to 1.2.2** (`DEFAULT_INSTALL_SPEC`; the Install action
  uses this).
- For export only: a second npm package, `indigo-matter-bridge` — on the npm registry
  (0.5.0 onward), exact-pinned to **0.8.0** (`DEFAULT_INSTALL_SPEC`) — installed by its
  own menu item.
- Python dep: `websockets` (see `Contents/Server Plugin/requirements.txt`).
- The **Domio** iOS app for adding devices — optional: the plugin menu's *Commission
  device by setup code…* does the same job from Indigo.

## Documentation

**[Field Notes](https://simons-plugins.github.io/indigo-matter/)** — the guides, properly
typeset. **Start here.**

- **[№ 1 — What is Matter](https://simons-plugins.github.io/indigo-matter/what-is-matter.html)** —
  the protocol primer: transports and Thread, border routers, commissioning, fabrics and
  multi-admin, and what "uncertified" really means.
- **[№ 2 — The controller](https://simons-plugins.github.io/indigo-matter/controller.html)** —
  Matter in: the share model, adding and removing devices, what lands in Indigo, and
  troubleshooting.
- **[№ 3 — The bridge](https://simons-plugins.github.io/indigo-matter/bridge.html)** —
  Matter out: the separate bridge node, opt-in exports, pairing into an ecosystem, and
  what has and hasn't been validated.
- **[№ 4 — The Proving Ground](https://simons-plugins.github.io/indigo-matter/testing.html)** —
  how this plugin is tested: the unit suites (plugin + bridge node), the device zoo, the
  virtual matter.js fleet, and live validation on a production server.

Setup is covered above; the [full install guide](https://simons-plugins.github.io/indigo-matter/INSTALL.html)
has the "On another computer" mode, backups, upgrading and troubleshooting.

- **[`docs/DEVICE-NOTES.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/DEVICE-NOTES.md)** —
  behaviours of specific hardware that are not obvious from the spec: the Tapo
  reset holds (one keeps your fabrics, one wipes them), the BILRESA channel →
  endpoint map, and why a sleepy Thread device takes seconds to answer.

## Development

```bash
pytest          # full unit suite; matter-server (WebSocket) and Indigo are mocked
```

Developer reference, in the repo — not intended as user documentation:

- [`docs/ARCHITECTURE.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/ARCHITECTURE.md) — what every module does and why. **Read before changing one.**
- [`docs/adr/INDEX.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/adr/INDEX.md) — architecture decisions: what was chosen, what was rejected, and what must not be revisited.
- [`docs/IMPLEMENTATION.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/IMPLEMENTATION.md) — protocols, scaffold, cluster handlers.
- [`docs/API.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/API.md) — the Domio ↔ plugin HTTP contract (v1.4).
- [`docs/BRIDGE_PROTOCOL.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/BRIDGE_PROTOCOL.md) — the plugin ⇄ bridge-node local protocol (export side).
- [`docs/PRD-indigo-matter-plugin.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/PRD-indigo-matter-plugin.md) — product requirements and milestones (inbound).
- [`docs/PRD-indigo-matter-export.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/PRD-indigo-matter-export.md) — the same for the Matter bridge.
- [`bridge-node/`](https://github.com/simons-plugins/indigo-matter/tree/main/bridge-node) — the bridge node itself (TypeScript; the only place matter.js is imported). `npm run build && npm test`.
- [`CLAUDE.md`](https://github.com/simons-plugins/indigo-matter/blob/main/CLAUDE.md) — orientation and workspace conventions (the per-module detail moved to `docs/ARCHITECTURE.md`).

## Trademarks and certification

This plugin is an **independent, unofficial integration**. It is **not certified** by the
Connectivity Standards Alliance, and has no affiliation with, sponsorship from, or
endorsement by the CSA.

"Matter" and the Matter logo are trademarks of the Connectivity Standards Alliance. They
are *certification* marks — on a certified product they signal that it passed CSA
testing. This plugin has not been through that process, so nothing here should be read as
a claim that it has. For contrast, Home Assistant may display Matter branding because it
holds two CSA certifications (Home Assistant as a certified user interface component, and
the Open Home Foundation Matter Server as a certified software component).

What that means in practice:

- The plugin speaks the Matter protocol via [`matter-server`](https://github.com/matter-js/matterjs-server)
  and works with real, certified devices — but it has not been tested against the CSA's
  certification suite, so device-by-device interoperability is not guaranteed.
- Commissioning uses the **share model**, so the device is first commissioned by a
  certified ecosystem (Apple Home, Alexa, Google Home); this plugin joins as a second
  admin. Your device's own certification is unaffected by anything here.
- The optional **Allow test/development device certificates** setting relaxes device
  attestation so uncertified devices (development boards, Homebridge/Matterbridge
  bridges) can be commissioned. It is off by default, warns on every start, and should
  be turned off once the device is paired.
- **The Matter bridge is uncertified in the other direction, and visibly so.** It
  advertises with the specification's test vendor ID, so every ecosystem shows an
  "uncertified accessory" warning when you add it and you choose *Add Anyway*. That is the
  normal state for this class of software — Homebridge, matterbridge and Home Assistant's
  bridge all ship the same way — and it is not something the plugin can change: outbound,
  the trust policy belongs to the ecosystem doing the commissioning.

The icon bundles the Matter symbol from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Logo_of_Matter_connectivity_standard.svg),
which is public domain for copyright purposes. See
[docs/branding/README.md](https://github.com/simons-plugins/indigo-matter/blob/main/docs/branding/README.md) for provenance.

## License

MIT — see [LICENSE](https://github.com/simons-plugins/indigo-matter/blob/main/LICENSE).
