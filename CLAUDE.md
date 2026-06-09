# CLAUDE.md — indigo-matter

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: Matter (indigo-matter)
- **Type**: Indigo plugin
- **Shortcut**: `matter`
- **GitHub**: https://github.com/simons-plugins/indigo-matter
- **Language**: Python 3.11+ (`CFBundleIdentifier` `com.simon.indigo-matter`)

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
- Workspace **ADR-0006** (`domio-code/docs/adr/`, = workspace `docs/adr/0004`) — the Matter architecture decision (Apple TV TBR + matter-server + multi-admin handoff).

## Architecture (this plugin)

Two async subsystems run on a **single asyncio event loop** on a dedicated
background thread (`async_runtime.py`); Indigo's `runConcurrentThread` is a non-I/O
watchdog. Indigo→loop bridges via `runtime.submit(coro).result(timeout)`;
loop→Indigo writes go straight through `device_sync.apply_states` (thread-safe IPC).

| Module | Role |
|---|---|
| `plugin.py` | Lifecycle glue, action bridge, IWS HTTP handlers |
| `async_runtime.py` | The event loop + thread + bridge primitives |
| `protocol.py` | **Rename firewall** — the only place that knows matter-server wire field names |
| `matter_client.py` | Persistent reconnecting WS client (uses `websockets`) |
| `server_process.py` | matter-server LaunchAgent (PM-B), gated by `manageLaunchAgent` pref (default off) |
| `commission_jobs.py` | Commissioning job state machine (API.md §3.2/§3.3) |
| `device_sync.py` | Node↔Indigo reconciliation + state/command seams |
| `http_handlers.py` | Domio API routing (served over IWS, not aiohttp) |
| `matter_model.py` | Parse matter-server node dict → node/endpoint objects |
| `matter_handlers/` | One `ClusterHandler` per cluster + registry (OnOff in v1) |

**HTTP transport:** the Domio API is served by the **Indigo Web Server** as
`<Action uiPath="hidden">` handlers at `…/message/com.simon.indigo-matter/{handler}`,
reached over the Reflector with the same Bearer API key Domio already uses — **not** a
standalone server (aiohttp is absent from Indigo's framework Python). See `docs/API.md` v1.1.

**matter-server:** npm package `matter-server` (GitHub `matter-js/matterjs-server`),
**Alpha** v0.6.2. Thread commissioning is non-functional in v0.6.2 — **Wi-Fi devices only**
for now; the WS client is portable to python-matter-server as the Thread fallback. See
`docs/IMPLEMENTATION.md` §2.7.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points:

- **Version bump per PR**: `Info.plist` `PluginVersion` (format `YYYY.R.P`, currently `2026.0.1`).
- **Testing**: `pytest` (`pyproject.toml`, pylint + 120-char like netro). matter-server is mocked at the WS layer (`tests/fakes.py`); the `indigo` module is mocked. Run: `cd indigo-matter && pytest`.
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead.

## Status

**M0–M4 built and unit-tested** (skeleton, async runtime, protocol adapter, WS client,
HTTP/commission API, OnOff cluster + device sync). On-device validation (M4 with a Tapo
P125M on jarvis) and M5–M8 (level/color, sensors, thermostat, failure hardening) are the
next milestones — see `docs/PRD-indigo-matter-plugin.md` §11.

Before writing plugin code, invoke `/indigo:dev`. Always `date -u` for timestamps.
