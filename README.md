# indigo-matter

Matter device support for the [Indigo](https://www.indigodomo.com) home automation server.

Add Matter devices from the **Domio** iOS app or the plugin's own menu; control them as
first-class Indigo devices (triggers, schedules, action groups, control pages, API). The
plugin manages a [`matter-server`](https://github.com/matter-js/matterjs-server) instance,
holds the Indigo-owned Matter fabric, translates Matter clusters ↔ Indigo device types,
and owns runtime control for each device's lifetime.

> **This is an independent, uncertified implementation.** It is not certified by the
> Connectivity Standards Alliance (CSA), and is not affiliated with or endorsed by them.
> It works, and is validated against real hardware — but it carries none of the
> interoperability guarantees that come with a certified Matter controller. See
> [Trademarks and certification](#trademarks-and-certification).

```text
Domio (iOS) ─┐
             ├── setup code ──▶ indigo-matter ──WebSocket──▶ matter-server ──IP──▶ Wi-Fi + Thread devices
plugin menu ─┘
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

## Status

**Built and live-validated, including with real hardware.** The full device-class
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
[Field Notes № 1](https://simons-plugins.github.io/indigo-matter/matter.html) for how the pieces
fit; validation specifics live in
[Handover](https://simons-plugins.github.io/indigo-matter/HANDOVER.html).

## Setup

The plugin doesn't speak Matter directly — it drives **matter-server**, a separate
Node.js process holding the Indigo-owned fabric. In local mode the plugin installs and
manages it for you, so setup is three steps and no Terminal beyond installing Node:

**1. Install Node.js 22** (≥ 22.13.0):

```bash
brew install node@22
brew link node@22
node --version          # confirm v22.x
```

Leave **Node bin directory** blank in the plugin config — auto-detect finds Homebrew
first. (nvm works too; pin `nodeBinDir` explicitly if you use it.)

**2. Install matter-server:** **Plugins ▸ Matter ▸ Install/update matter-server**. This
installs the pinned version with the *same* Node it will run the server with, and pins
that Node — which avoids the most common failure, installing with one Node and running
with another whose native modules won't load.

**3. Verify** — the Indigo event log should show:

```text
connected to matter-server, listening
reconciled N Matter node(s)
```

`N` is 0 on a fresh install; that's fine. If you instead see
`Connect call failed ('127.0.0.1', 5580)`, matter-server isn't running — that, plus
manual mode, fabric backups, upgrading and uninstalling, is covered in the
[full install guide](https://simons-plugins.github.io/indigo-matter/INSTALL.html).

## Requirements

- Indigo 2025.2+ on macOS.
- **Node.js ≥ 22.13.0** (Homebrew is the paved road; nvm works too) and the `matter-server`
  npm package — Beta, **exact-pinned to 1.2.2** (`DEFAULT_INSTALL_SPEC`; the Install action
  uses this).
- Python dep: `websockets` (see `Contents/Server Plugin/requirements.txt`).
- The **Domio** iOS app for adding devices — optional: the plugin menu's *Commission
  device by setup code…* does the same job from Indigo.

## Documentation

**[Field Notes](https://simons-plugins.github.io/indigo-matter/)** — the guides, properly
typeset. **Start here.**

- **[№ 1 — The Landscape](https://simons-plugins.github.io/indigo-matter/matter.html)** —
  Matter & Thread explained: how Indigo, Domio, matter-server and Apple Home fit together,
  what to do when you don't have Apple Home, sharing with other platforms, firmware, and
  troubleshooting.
- **[№ 2 — The Proving Ground](https://simons-plugins.github.io/indigo-matter/testing.html)** —
  how this plugin is tested: the unit suite, the device zoo, the virtual matter.js fleet,
  and live validation on a production server.

Setup is covered above; the [full install guide](https://simons-plugins.github.io/indigo-matter/INSTALL.html)
has manual mode, backups, upgrading and troubleshooting.

## Development

```bash
pytest          # full unit suite; matter-server (WebSocket) and Indigo are mocked
```

Developer reference, in the repo — not intended as user documentation:

- [`docs/IMPLEMENTATION.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/IMPLEMENTATION.md) — protocols, scaffold, cluster handlers.
- [`docs/API.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/API.md) — the Domio ↔ plugin HTTP contract (v1.3).
- [`docs/PRD-indigo-matter-plugin.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/PRD-indigo-matter-plugin.md) — product requirements and milestones.
- [`CLAUDE.md`](https://github.com/simons-plugins/indigo-matter/blob/main/CLAUDE.md) — architecture and workspace conventions.

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

The icon bundles the Matter symbol from
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Logo_of_Matter_connectivity_standard.svg),
which is public domain for copyright purposes. See
[docs/branding/README.md](https://github.com/simons-plugins/indigo-matter/blob/main/docs/branding/README.md) for provenance.

## License

MIT — see [LICENSE](https://github.com/simons-plugins/indigo-matter/blob/main/LICENSE).
</content>
