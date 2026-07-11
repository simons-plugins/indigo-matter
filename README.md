# indigo-matter

Matter device support for the [Indigo](https://www.indigodomo.com) home automation server.

Add Matter devices from the **Domio** iOS app; control them as first-class Indigo
devices (triggers, schedules, action groups, control pages, API). The plugin manages a
[`matter-server`](https://github.com/matter-js/matterjs-server) instance, holds the
Indigo-owned Matter fabric, and translates Matter clusters ↔ Indigo device types.

```
Domio (iOS) ──share code──▶ indigo-matter ──WebSocket──▶ matter-server ──IP──▶ Wi-Fi Matter devices
```

Commissioning uses the **share model**: Apple Home commissions the
device first, Domio relays a share/pairing code to the plugin, and the plugin joins the
device's fabric as a second admin over IP. The plugin owns runtime control for the
device's lifetime.

## Status

**Built and live-validated, including with real hardware.** The full device-class
catalogue is shipped — lighting (on/off/dimmer/colour), sensors, thermostats, fans,
window coverings, locks, valves, buttons, smoke/CO, air quality, energy metering,
battery levels, and bridges — plus failure hardening, fabric backup/restore, and a
managed launchd LaunchAgent for matter-server. A real Tapo energy plug has been
commissioned end-to-end through Domio and reports live power/energy to Indigo.

**Validated with both Wi-Fi and Thread devices** through the same share model — the
admin-1 ecosystem (Apple Home, or equally Alexa / Google Home) does the Thread
provisioning; the plugin joins over IP via that ecosystem's border router
(HomePod/Apple TV, TBR-capable Echo, Nest Hub). See
[docs/MATTER.md](./docs/MATTER.md) for how the pieces fit; validation specifics live
in [docs/HANDOVER.md](./docs/HANDOVER.md).

## Setup

matter-server is a separate Node.js runtime. In local mode the plugin installs and
manages it for you — **Plugins ▸ Matter ▸ Install/update matter-server** installs the
pinned version with your Node and restarts onto it (no Terminal). See
**[docs/INSTALL.md](./docs/INSTALL.md)** for the full guide (Node, matter-server, the two
run modes, verification, backups, and troubleshooting).

## Requirements

- Indigo 2025.2+ on macOS.
- **Node.js ≥ 22.13.0** (Homebrew is the paved road; nvm works too) and the `matter-server`
  npm package — Beta, **exact-pinned to 1.2.2** (`DEFAULT_INSTALL_SPEC`; the Install action
  uses this).
- Python dep: `websockets` (see `Contents/Server Plugin/requirements.txt`).
- The **Domio** iOS app for adding devices.

## Documentation

- **[docs/MATTER.md](./docs/MATTER.md)** — Matter & Thread explained: how Indigo, Domio, matter-server and Apple Home fit together. **Start here.**
- **[docs/INSTALL.md](./docs/INSTALL.md)** — matter-server install & plugin setup.
- **[docs/TESTING.md](./docs/TESTING.md)** — how the plugin is tested: unit suite, the device zoo, the virtual matter.js fleet, and live validation.
- `docs/PRD-indigo-matter-plugin.md` — product requirements and milestones.
- `docs/IMPLEMENTATION.md` — protocols, scaffold, setup, cluster handlers.
- `docs/API.md` — the Domio ↔ plugin HTTP contract (v1.3, served over the Indigo Web Server).

## Development

```bash
pytest          # full unit suite; matter-server (WebSocket) and Indigo are mocked
```

See [CLAUDE.md](./CLAUDE.md) for architecture and workspace conventions.

## License

MIT — see [LICENSE](./LICENSE).
</content>
