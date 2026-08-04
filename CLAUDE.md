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

| Module | Role |
|---|---|
| `plugin.py` | Lifecycle glue, action bridge, IWS HTTP handlers |
| `async_runtime.py` | The event loop + thread + bridge primitives |
| `protocol.py` | **Rename firewall** — the only place that knows matter-server wire field names |
| `ws_json_client.py` | Shared transport core for both WS clients: run loop, `min(2**attempt, 30)` reconnect backoff, `message_id`→future correlation, disconnect/diagnostic handling. Handshake + frame vocabulary are subclass hooks; unmatched **error** responses are logged (BRIDGE_PROTOCOL §3.4) |
| `matter_client.py` | matter-server client (inbound): the `serverLocation` URI, the `server_info` + `start_listening` handshake, controller command wrappers |
| `bridge_protocol.py` | Bridge-node wire contract — envelope/commands/error codes/roles + normalised `BridgeCommand`/`StatusReport`/`PairingReport`/`FabricInfo`. **Not** a rename firewall (we own both ends); `protocolVersion` is what protects us. See `docs/BRIDGE_PROTOCOL.md` |
| `bridge_client.py` | Bridge-node client (outbound export): hello+attach handshake that fails closed on version skew, endpoint CRUD, fire-and-forget `set_state`, §5 event callbacks. Attach refusals are triaged (§1.1): `version_mismatch`/`mass_removal_refused` halt with a `halted_reason`, `endpoint_map_invalid` holds the socket open un-attached in a `recovery` state so the §3.11 rebuild stays reachable, anything else reconnects on the normal backoff |
| `export_store.py` | The export allow-list (PRD-indigo-matter-export §5.1): `ExportEntry` (device id + role + name override + options) and an `RLock`'d store persisted as ONE JSON string in `pluginPrefs["matterExports"]`, schema-versioned. A blob it cannot parse is moved aside to `matterExports.corrupt` and the store starts empty — user config is never silently discarded |
| `export_catalog.py` | Indigo device → eligible Matter roles, or an `Excluded(reason)` shown in the picker (PRD §5.2, XAC9). The loop guard (XNG3/XAC6) is `pluginId` and nothing else, checked before any type reasoning. Type dispatch walks the IOM **class-name chain**, not `isinstance` — the indigo module is a MagicMock under test |
| `launch_agent.py` | Generic launchd LaunchAgent machinery (npm/npx/node resolution, plist authoring, applied-plist digest, orphan/EADDRINUSE reaping), driven by a frozen `AgentSpec` that carries one agent's identity. Extracted so the Matter **bridge node** can be a second agent without duplicating it (PRD-indigo-matter-export §4.2 / XOQ3) |
| `server_process.py` | `ServerProcess` = the matter-server (controller) specialisation of `LaunchAgent`: its prefs, its argv, its pinned version. Gated by the `serverLocation` pref — the config asks "is matter-server on this Mac?"; `local` (turnkey default) manages it here on loopback, `remote` connects to a server elsewhere. `manageLaunchAgent`/host/port are derived from that in `startup` (see `plugin.py:server_location`) |
| `commission_jobs.py` | Commissioning job state machine (API.md §3.2/§3.3) |
| `device_sync.py` | Node↔Indigo reconciliation + state/command seams |
| `fabric_backup.py` | Fabric backup/restore for the matter-server storage dir (issue #26) — zip snapshots into a sibling `backups/`, move-aside restore, retention prune. Pure + injectable (paths, clock, `stop()/start()` control) so it unit-tests against `tmp_path` |
| `http_handlers.py` | Domio API routing (served over IWS, not aiohttp) |
| `matter_model.py` | Parse matter-server node dict → node/endpoint objects |
| `matter_handlers/` | One `ClusterHandler` per cluster + registry (OnOff in v1) |

**HTTP transport:** the Domio API is served by the **Indigo Web Server** as
`<Action uiPath="hidden">` handlers at `…/message/com.simons-plugins.indigo-matter/{handler}`,
reached over the Reflector with the same Bearer API key Domio already uses — **not** a
standalone server (aiohttp is absent from Indigo's framework Python). See `docs/API.md` v1.1.

**matter-server:** npm package `matter-server` (GitHub `matter-js/matterjs-server`),
**Beta**, exact-pinned to **1.2.2** (`DEFAULT_INSTALL_SPEC`; the Install menu action installs
this; requires Node ≥ 22.13.0). jarvis field-validated 1.1.2 and 1.2.2. Its
one Thread gap is *first-admin* commissioning (provisioning Thread
credentials over BLE) — which the share model never does. **Wi-Fi and Thread both
validated with real hardware** (Tapo P110M over Wi-Fi; Aqara FP300 over Thread via a
HomePod TBR — the admin-1 ecosystem provisions; plugin joins over
IP through that ecosystem's TBR). WS
client is portable to python-matter-server as a fallback. See `docs/MATTER.md` +
`docs/IMPLEMENTATION.md` §3.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points:

- **Version bump per PR**: `Info.plist` `PluginVersion` (format `YYYY.R.P`; the plist is the source of truth for the current value).
- **Testing**: `pytest` (`pyproject.toml`, pylint + 120-char like netro). matter-server is mocked at the WS layer (`tests/fakes.py`); the `indigo` module is mocked. Run: `cd indigo-matter && pytest`. The bridge node has its own suite: `cd bridge-node && npm run build && npm test`.
- **Bridge-protocol golden frames**: `tests/fixtures/bridge_protocol/frames.json` is the ONE shared fixture file (BRIDGE_PROTOCOL §7) — the Python suite reads it directly, `npm test` copies it into the TS build. Change a frame and both suites must be updated, by design.
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead.

## Status

**M0–M4 built and unit-tested** (skeleton, async runtime, protocol adapter, WS client,
HTTP/commission API, OnOff cluster + device sync). On-device validation (M4 with a Tapo
P125M on jarvis) and M5–M8 (level/color, sensors, thermostat, failure hardening) are the
next milestones — see `docs/PRD-indigo-matter-plugin.md` §11.

Before writing plugin code, invoke `/indigo:dev`. Always `date -u` for timestamps.
