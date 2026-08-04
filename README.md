# indigo-matter

Matter device support for the [Indigo](https://www.indigodomo.com) home automation server.

Add Matter devices from the **Domio** iOS app or the plugin's own menu; control them as
first-class Indigo devices (triggers, schedules, action groups, control pages, API). The
plugin manages a [`matter-server`](https://github.com/matter-js/matterjs-server) instance,
holds the Indigo-owned Matter fabric, and translates Matter clusters ↔ Indigo device types.

```text
Domio (iOS) ─┐
             ├── setup code ──▶ indigo-matter ──WebSocket──▶ matter-server ──IP──▶ Wi-Fi + Thread devices
plugin menu ─┘
```

Devices can be added from **either** entry point — the **Domio** iOS app, or the plugin
menu's *Commission device by setup code…* — and the two are equivalent, for Wi-Fi and
Thread alike.

Most devices arrive via the **share model**: an ecosystem you already own (Apple Home, or
equally Alexa / Google Home) does the out-of-box Bluetooth onboarding — the one step a
headless Mac can't, since matter-server runs without BLE — and the device is then shared
to Indigo over plain IP, where the plugin joins as a second admin. A **factory-fresh
Thread** device can be added through Domio because iOS commissions it to the iPhone as
part of that flow, which is what supplies the Thread credentials. A device already on
your network that was never commissioned by anyone can be added **directly** with its
printed code, from either entry point.

However it was added, the plugin owns runtime control for the device's lifetime.

> **This is an independent, uncertified implementation.** It is not certified by the
> Connectivity Standards Alliance (CSA), and is not affiliated with or endorsed by them.
> It works, and is validated against real hardware — but it carries none of the
> interoperability guarantees that come with a certified Matter controller. See
> [Trademarks and certification](#trademarks-and-certification).

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
[Matter](https://simons-plugins.github.io/indigo-matter/MATTER.html) for how the pieces fit; validation specifics live
in [Handover](https://simons-plugins.github.io/indigo-matter/HANDOVER.html).

## Setup

matter-server is a separate Node.js runtime. In local mode the plugin installs and
manages it for you — **Plugins ▸ Matter ▸ Install/update matter-server** installs the
pinned version with your Node and restarts onto it (no Terminal). See
**[Install](https://simons-plugins.github.io/indigo-matter/INSTALL.html)** for the full guide (Node, matter-server, the two
run modes, verification, backups, and troubleshooting).

## Requirements

- Indigo 2025.2+ on macOS.
- **Node.js ≥ 22.13.0** (Homebrew is the paved road; nvm works too) and the `matter-server`
  npm package — Beta, **exact-pinned to 1.2.2** (`DEFAULT_INSTALL_SPEC`; the Install action
  uses this).
- Python dep: `websockets` (see `Contents/Server Plugin/requirements.txt`).
- The **Domio** iOS app for adding devices — optional: the plugin menu's *Commission
  device by setup code…* does the same job from Indigo.

## Documentation

**[Field Notes](https://simons-plugins.github.io/indigo-matter/)** — longer-form write-ups,
published as a site:

- **[№ 1 — The Landscape](https://simons-plugins.github.io/indigo-matter/matter.html)** —
  Matter, Thread, and where Indigo fits.
- **[№ 2 — The Proving Ground](https://simons-plugins.github.io/indigo-matter/testing.html)** —
  how this plugin is tested.

Reference documentation:

- **[Matter](https://simons-plugins.github.io/indigo-matter/MATTER.html)** — Matter & Thread explained: how Indigo, Domio, matter-server and Apple Home fit together. **Start here.**
- **[Install](https://simons-plugins.github.io/indigo-matter/INSTALL.html)** — matter-server install & plugin setup.
- **[Testing](https://simons-plugins.github.io/indigo-matter/TESTING.html)** — how the plugin is tested: unit suite, the device zoo, the virtual matter.js fleet, and live validation.
- **[PRD](https://simons-plugins.github.io/indigo-matter/PRD-indigo-matter-plugin.html)** — product requirements and milestones.
- **[Implementation](https://simons-plugins.github.io/indigo-matter/IMPLEMENTATION.html)** — protocols, scaffold, setup, cluster handlers.
- **[API](https://simons-plugins.github.io/indigo-matter/API.html)** — the Domio ↔ plugin HTTP contract (v1.3, served over the Indigo Web Server).

## Development

```bash
pytest          # full unit suite; matter-server (WebSocket) and Indigo are mocked
```

See [CLAUDE.md](https://github.com/simons-plugins/indigo-matter/blob/main/CLAUDE.md) for architecture and workspace conventions.

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
