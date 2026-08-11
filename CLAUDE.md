# CLAUDE.md — indigo-matter

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: Matter (indigo-matter)
- **Type**: Indigo plugin
- **Shortcut**: `matter`
- **GitHub**: https://github.com/simons-plugins/indigo-matter
- **Language**: Python 3.11+ (`CFBundleIdentifier` `com.simons-plugins.indigo-matter`)

## Role in the workspace

Brings Matter device support to Indigo. Manages a `matter-server` instance (Node,
python-matter-server-compatible WebSocket protocol), holds the Indigo fabric, and
translates Matter clusters ↔ Indigo device types so Matter devices behave as
first-class Indigo devices. **Domio owns commissioning UX** (one-shot per device);
this plugin owns runtime control for the device's lifetime.

```
Domio (iOS, MatterSupport) ──commission+handoff──▶ indigo-matter ──WebSocket──▶ matter-server ──▶ Apple TV (TBR) ──▶ Thread/Wi-Fi devices
        │                                                  ▲
        └──────────── HTTP API (API.md, via IWS) ──────────┘
```

## Related projects

- [`../domio-code/`](../domio-code/) — iOS app; owns commissioning. The Domio↔plugin HTTP contract is **`docs/API.md`** (mirrored in both repos; keep in sync).
- The Matter architecture decision (the **share model**: the user's existing ecosystem commissions as admin 1, the plugin joins as a second admin over IP, the ecosystem's hub is the Thread border router) is explained for users in [`docs/MATTER.md`](./docs/MATTER.md).

## Architecture (this plugin)

Two async subsystems run on a **single asyncio event loop** on a dedicated
background thread (`async_runtime.py`); Indigo's `runConcurrentThread` is a non-I/O
watchdog. Indigo→loop bridges via `runtime.submit(coro).result(timeout)`;
loop→Indigo writes go straight through `device_sync.apply_states` (thread-safe IPC).

Two directions, sharing almost nothing:

- **Inbound** — Matter devices Indigo controls. `matter_client.py` speaks to a
  `matter-server` Node process; `device_sync.py` reconciles nodes into Indigo
  devices; `matter_handlers/` holds one handler per cluster.
- **Outbound** — Indigo devices exported as Matter accessories. `export_bridge.py`
  drives a plugin-owned matter.js node in `bridge-node/`, the **only** place
  matter.js is imported (workspace ADR-0006).

`plugin.py` is lifecycle glue and the device/action bridge; everything Indigo
reaches by XML-named callback lives in one of five mixins, because Indigo resolves
those names as attributes on the `Plugin` class (issue #146).

**→ Per-module detail is in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).**
Read it before changing a module — most rows record a decision reached the hard
way and say why the obvious alternative was rejected.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points:

- **Version bump per PR**: `Info.plist` `PluginVersion` (format `YYYY.R.P`; the plist is the source of truth for the current value).
- **Testing**: `pytest` (`pyproject.toml`, pylint + 120-char like netro). matter-server is mocked at the WS layer (`tests/fakes.py`); the `indigo` module is mocked. Run: `cd indigo-matter && pytest`. The bridge node has its own suite: `cd bridge-node && npm run build && npm test`.
- **Bridge-protocol golden frames**: `tests/fixtures/bridge_protocol/frames.json` is the ONE shared fixture file (BRIDGE_PROTOCOL §7) — the Python suite reads it directly, `npm test` copies it into the TS build. Change a frame and both suites must be updated, by design.
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead.

## Status

Both directions are built, released, and field-validated against real hardware:
inbound control over Wi-Fi (Tapo P110M) and Thread (Aqara FP300 via a HomePod
border router), and outbound export of Indigo devices as Matter accessories
through the bridge node. The current version is in `Info.plist`, which is the
source of truth.

Work is issues-driven — read the GitHub issue list rather than a milestone plan.
`docs/PRD-indigo-matter-plugin.md` and `docs/PRD-indigo-matter-export.md` record
the original scope of each direction; both are historical.

Before writing plugin code, invoke `/indigo:dev`. Always `date -u` for timestamps.
