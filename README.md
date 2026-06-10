# indigo-matter

Matter device support for the [Indigo](https://www.indigodomo.com) home automation server.

Add Matter devices from the **Domio** iOS app; control them as first-class Indigo
devices (triggers, schedules, action groups, control pages, API). The plugin manages a
[`matter-server`](https://github.com/matter-js/matterjs-server) instance, holds the
Indigo-owned Matter fabric, and translates Matter clusters ↔ Indigo device types.

```
Domio (iOS) ──share code──▶ indigo-matter ──WebSocket──▶ matter-server ──IP──▶ Wi-Fi Matter devices
```

Commissioning uses the **share model** (ADR-0006, "C4"): Apple Home commissions the
device first, Domio relays a share/pairing code to the plugin, and the plugin joins the
device's fabric as a second admin over IP. The plugin owns runtime control for the
device's lifetime.

## Status

**v1 — built and live-validated.** Milestones M0–M8 are complete and verified on the
reference Indigo server: plugin skeleton, async runtime, matter-server WebSocket client,
the Domio HTTP API (contract **v1.3**), OnOff/dimmer/color, sensors, thermostat, and
failure hardening. The plugin can supervise matter-server itself via a managed launchd
LaunchAgent. **Wi-Fi Matter devices only** for now — matter-server (Alpha v0.6.2) doesn't
yet support Thread commissioning, and BLE is not enabled on macOS.

## Setup

matter-server is a separate Node.js runtime that you install once. See
**[docs/INSTALL.md](./docs/INSTALL.md)** for the full install and configuration guide
(Node 22, matter-server, the two run modes, verification, backups, and troubleshooting).

## Requirements

- Indigo 2025.2+ on macOS.
- Node.js 22 LTS (via nvm or Homebrew) and the `matter-server` npm package (Alpha; pinned).
- Python dep: `websockets` (see `Contents/Server Plugin/requirements.txt`).
- The **Domio** iOS app for adding devices.

## Documentation

- **[docs/INSTALL.md](./docs/INSTALL.md)** — matter-server install & plugin setup.
- `docs/PRD-indigo-matter-plugin.md` — product requirements and milestones.
- `docs/IMPLEMENTATION.md` — protocols, scaffold, setup, cluster handlers.
- `docs/API.md` — the Domio ↔ plugin HTTP contract (v1.3, served over the Indigo Web Server).

## Development

```bash
pytest          # ~225 unit tests; matter-server (WebSocket) and Indigo are mocked
```

See [CLAUDE.md](./CLAUDE.md) for architecture and workspace conventions.

## License

MIT — see [LICENSE](./LICENSE).
</content>
