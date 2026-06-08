# indigo-matter

Matter device support for the [Indigo](https://www.indigodomo.com) home automation server.

Commission Matter devices from the **Domio** iOS app; control them as first-class Indigo
devices (triggers, schedules, action groups, control pages, API). The plugin manages a
[`matter-server`](https://github.com/matter-js/matterjs-server) instance and translates
Matter clusters ↔ Indigo device types.

```
Domio (iOS) ──commission──▶ indigo-matter ──WebSocket──▶ matter-server ──▶ Apple TV (Thread Border Router) ──▶ devices
```

## Status

**v1 (in development).** The M0–M4 validation slice is built and unit-tested: plugin
skeleton, async runtime, matter-server WebSocket client, the Domio HTTP API, and the
OnOff (relay) cluster end-to-end. Wi-Fi Matter devices only for now — matter-server
v0.6.2 doesn't yet support Thread commissioning.

## Requirements

- Indigo 2024.1+ on macOS.
- Node.js 22 LTS and the `matter-server` npm package (Alpha; pin a version).
- An Apple TV (or other Thread Border Router) for Thread devices.
- Python dep: `websockets` (see `Contents/Server Plugin/requirements.txt`).

## Documentation

- `docs/PRD-indigo-matter-plugin.md` — product requirements and milestones.
- `docs/IMPLEMENTATION.md` — protocols, scaffold, setup, cluster handlers.
- `docs/API.md` — the Domio ↔ plugin HTTP contract (v1.1, served over the Indigo Web Server).

## Development

```bash
pytest          # unit tests (matter-server + Indigo are mocked)
```

See [CLAUDE.md](./CLAUDE.md) for architecture and workspace conventions.

## License

TBD.
