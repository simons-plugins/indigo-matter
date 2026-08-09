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
| `plugin.py` | Lifecycle glue and the device/action bridge — `startup`/`shutdown`/`runConcurrentThread`, the device-subscription/export-id watchdog, `deviceUpdated`/`deviceDeleted`, `actionControl*`, and `getPrefsConfigUiValues`'s PRD §5.5 export readout. Composes the four mixins below into `Plugin`; Indigo resolves every `<CallbackMethod>`/list-method by name as an attribute on the `Plugin` class, which is why the split (issue #146) is mixins rather than free-standing modules — none of them may define `__init__`/`deviceUpdated`/etc., or the `super()` chain to `indigo.PluginBase` breaks |
| `plugin_constants.py` | Constants (timeouts, menu/option labels) and the two pure prefs helpers, `server_location`/`sanitize_host`, shared across `plugin.py` and the four mixins — split out because several groups are consumed by two different mixins each, and a mixin importing from `plugin.py` would be a back-import (issue #146) |
| `pairing_page.py` | The pairing IWS page's HTML template (PRD §6) — pure rendering, no `indigo` import, no plugin state. Only caller is `HttpApiMixin._pairing_page` |
| `http_api_mixin.py` | The Domio HTTP API (`docs/API.md` v1.1) — `http_status`/`http_commission`/`http_decommission`/`http_diagnostics`/`http_pairing`. All five IWS `<Action uiPath="hidden">` handlers share `_parse_request`/`_reply`, which is why the pairing page's IWS glue lives here rather than in `PairingMenuMixin` |
| `export_dialog_mixin.py` | The **Manage Matter Exports…** dialog (PRD-indigo-matter-export §5.1 UI-D) — candidate/role pickers, add/update/remove, and the reconcile/save machinery `startup` calls into. `_truthy` is a de facto shared helper used by call sites across three mixins; it lives here only because `Plugin` always composes all four |
| `pairing_menu_mixin.py` | **Pair Matter Bridge…** (§3.8, codes to the event log because Indigo dialogs have no dynamic labels), **Unpair an Ecosystem…** (§3.9, two confirm gates + a dynamic fabric picker), and fabric backup/restore (issues #26, #136) |
| `server_menu_mixin.py` | matter-server install/restart/decommission menus, export-bridge recovery (rebuild endpoint map, reset pairings), and the bridge node's LaunchAgent start/stop/diagnose seams — three originally separate bands kept in one mixin because they share one construction rule (`ServerProcess` only ever from `Plugin._server_prefs()`) and one install thread |
| `async_runtime.py` | The event loop + thread + bridge primitives |
| `protocol.py` | **Rename firewall** — the only place that knows matter-server wire field names |
| `ws_json_client.py` | Shared transport core for both WS clients: run loop, `min(2**attempt, 30)` reconnect backoff, `message_id`→future correlation, disconnect/diagnostic handling. Handshake + frame vocabulary are subclass hooks; unmatched **error** responses are logged (BRIDGE_PROTOCOL §3.4) |
| `matter_client.py` | matter-server client (inbound): the `serverLocation` URI, the `server_info` + `start_listening` handshake, controller command wrappers |
| `bridge_protocol.py` | Bridge-node wire contract — envelope/commands/error codes/roles + normalised `BridgeCommand`/`StatusReport`/`PairingReport`/`FabricInfo`. **Not** a rename firewall (we own both ends); `protocolVersion` is what protects us. See `docs/BRIDGE_PROTOCOL.md` |
| `bridge_client.py` | Bridge-node client (outbound export): hello+attach handshake that fails closed on version skew, endpoint CRUD, fire-and-forget `set_state`, §5 event callbacks. Attach refusals are triaged (§1.1): `version_mismatch`/`mass_removal_refused` halt with a `halted_reason`, `endpoint_map_invalid` holds the socket open un-attached in a `recovery` state so the §3.11 rebuild stays reachable, anything else reconnects on the normal backoff |
| `export_store.py` | The export allow-list (PRD-indigo-matter-export §5.1): `ExportEntry` (device id + role + name override + options) and an `RLock`'d store persisted as ONE JSON string in `pluginPrefs["matterExports"]`, schema-versioned. A blob it cannot parse is moved aside to `matterExports.corrupt` and the store starts empty — user config is never silently discarded |
| `export_catalog.py` | Indigo device → eligible Matter roles, or an `Excluded(reason)` shown in the picker (PRD §5.2, XAC9). The loop guard (XNG3/XAC6) is `pluginId` and nothing else, checked before any type reasoning. Type dispatch walks the IOM **class-name chain**, not `isinstance` — the indigo module is a MagicMock under test |
| `export_handlers.py` | The **outbound** handler table, keyed by §4.2 **role** (the inbound registry is keyed by cluster; outbound there is no cluster, only a user-declared role) — `states_for` / `diff` / `dispatch` per role, each taking the export's §4.1 `options`. **Total over the v1 role enum since E4**: plug, on/off light, dimmable, colour-temp, extended colour, covering, lock, the seven sensors, thermostat. Hue diffs carry a ±1° tolerance (Matter's 0–254 hue round-trips ±1°); saturation deliberately has none. `windowCovering` applies the per-export `invert` polarity here so a `position` on the wire always means 100 = open; `doorLock` dispatches `indigo.device.lock`/`unlock` and confirms **nothing** (PRD §7). Indigo declares no units, so sensor/thermostat readings are passed through as already being in the §4.2 unit — documented in the module header as the known gap the device catalog should close. **One exception, `pressureSensor`:** Indigo's barometer convention is hPa (this plugin's own inbound handler writes it, and `export_catalog` routes `hpa`/`mbar` names here) and §4.2's key is `pressureKPa`, so it divides by 10 |
| `export_bridge.py` | The outbound engine: owns the `BridgeClient` and everything the Indigo callbacks *mean* for it. The client exists **only** while the allow-list is non-empty (XG5) and starts/stops on the dialog's empty↔non-empty transitions — and since E7 so does the **bridge LaunchAgent**, through injected `agent_start`/`agent_stop`/`agent_diagnose` seams: started before the client on empty→non-empty, stopped **after** the un-export has landed on non-empty→empty, and never stopped by a session that did not start it. The §5 pairing events (`fabrics_changed`/`commissioned`/`decommissioned`/`window_closed`) are consumed here since E6 — they were emitted by the node from E5 and read by nobody. PRD §5.5's `exportEnabled` switch fails **open** (absent or null means on, the opposite of the controller's attestation flag) and turning it off deliberately does **not** un-export: it drops the socket and stops the agent, leaving accessories paired-but-unavailable. The attach endpoint provider **re-runs `export_catalog.classify` on every attach** — the store is a past user declaration, not a guard — and skips-with-warning anything deleted/excluded/re-typed or carrying a role this build has no handler for — which since E4 means only an allow-list written by a *newer* plugin (an unknown role fails the *whole* attach). State pushes are fire-and-forget onto the loop; `on_command` dispatches `indigo.*` from the loop thread, the same discipline `device_sync.apply_states` already uses |
| `launch_agent.py` | Generic launchd LaunchAgent machinery (npm/npx/node resolution, plist authoring, applied-plist digest, orphan/EADDRINUSE reaping), driven by a frozen `AgentSpec` that carries one agent's identity. Extracted so the Matter **bridge node** can be a second agent without duplicating it (PRD-indigo-matter-export §4.2 / XOQ3). Since E7 `remove_package` is **per package** (`npm uninstall <name>`, falling back to deleting only `node_modules/<package>`) — it used to rmtree the shared `node_modules` and delete `package-lock.json`, which with two agents took the sibling's package out from under a still-loaded job. The `.indigo-node` install stamp stays deliberately **shared** (one node runs both agents) |
| `bridge_agent.py` | The **export bridge node**'s LaunchAgent (E7) — the second `AgentSpec`: label `com.simons-plugins.indigo-matter.bridge`, package `indigo-matter-bridge` (registry spec, exact-pinned in `DEFAULT_INSTALL_SPEC`), entry `dist/main.js`, its own logs, per-label applied-digest marker, and an argv builder for `--storage-path`/`--ws-port`/`--matter-port`/`--mdns-interface`. Its storage dir is **derived, not configured** — `bridge_storage_path()` is the one derivation the agent, the fabric backup and `config.ts`'s `DEFAULT_STORAGE_PATH` all have to agree on. Never constructed at `startup`: the agent is gated by the allow-list (XG5/XAC1), so a fresh install writes no plist and runs no process |
| `server_process.py` | `ServerProcess` = the matter-server (controller) specialisation of `LaunchAgent`: its prefs, its argv, its pinned version. Gated by the `serverLocation` pref — the config asks "is matter-server on this Mac?"; `local` (turnkey default) manages it here on loopback, `remote` connects to a server elsewhere. `manageLaunchAgent`/host/port are derived from that in `startup` (see `plugin.py:server_location`) |
| `commission_jobs.py` | Commissioning job state machine (API.md §3.2/§3.3) |
| `device_sync.py` | Node↔Indigo reconciliation + state/command seams |
| `fabric_backup.py` | Fabric backup/restore for the matter-server storage dir (issue #26) — zip snapshots into a sibling `backups/`, move-aside restore, retention prune. **Since E5 it also covers the export bridge node's storage dir** (`identity.json` + `endpoint-map.json`) under a reserved `bridge-node/` archive prefix, so an old controller-only archive still restores; a missing/empty bridge dir is skipped **with a WARNING naming the path**, because that path is derived from the controller's rather than configured. **Since #136, restore extracts the bridge members too** — it stops the bridge node (via the same `stop()`/`start()` seam, now given a second control), extracts its half alongside the controller fabric, and restarts it afterwards if it was running; a bridge-restart failure never rolls back an otherwise-good controller restore (XG5). It still falls back to reporting-and-skipping the bridge side (loudly, with the manual recipe) when there is no bridge control, no usable bridge storage path, or the path overlaps the controller's. Pure + injectable (paths, clock, two `stop()/start()` controls) so it unit-tests against `tmp_path` |
| `http_handlers.py` | Domio API routing (served over IWS, not aiohttp) |
| `matter_model.py` | Parse matter-server node dict → node/endpoint objects |
| `matter_handlers/` | One `ClusterHandler` per cluster + registry (OnOff in v1) |

### The bridge node (`bridge-node/src/`, TypeScript)

The export side's other half — a separate Node process, the **only** place
matter.js is imported (ADR-0006). Its wire contract is `docs/BRIDGE_PROTOCOL.md`.

| Module | Role |
|---|---|
| `main.ts` | Entry point: arg parsing, identity load, ordered SIGTERM shutdown. Exits **0** on a clean stop (launchd's `KeepAlive SuccessfulExit:false` reads the number) — `ServerNode.erase()` leaves a ref'd timer `close()` never clears, so a clean stop after a factory reset used to exit 1. An unusable `identity.json` is moved aside to `identity.json.unreadable-<stamp>` and a replacement is minted **in memory only** — never written over the `SerialNumber`/`UniqueID` every paired ecosystem knows |
| `node.ts` | The `ServerNode` + aggregator, the PRD §7 refuse-to-start decision, §3.9–§3.11, and the §5 event sinks |
| `endpoint-map.ts` | `endpoint-map.json` — the persisted `UniqueID → {number, role, label}` map (schema v2 since issue #141; v1 numbers-only files are migrated in place, never discarded) and its drift detector (PRD §4.3). Since #141 it is also what `node.ts` **restores the endpoint set from before `server.start()`**, so the bridge is never online with an empty aggregator — an empty `PartsList` made Apple re-create every accessory in the bridge's own room on every restart. **It does not allocate anything: matter.js owns the numbers**, keyed on `Endpoint.id` in its own store; this file is the independent *witness*, so a lost/reset matter.js storage becomes a log line instead of silently duplicating every accessory. Lives OUTSIDE matter.js's storage context on purpose (§3.10's reset wipes that). Report-only — drift is never repaired, or the next pass would call the same fault clean. A **commissioned bridge with no map at all bootstraps** a baseline from matter.js's own persisted numbers and serves (every pre-E5 install is in that state); only a *present-but-unreadable* map refuses. `refuseReasonFor` is a pure function with no matter.js import, because the case that matters most cannot be reached in a test without real hardware |
| `storage.ts` | `identity.json`: install id, passcode, discriminator, and the `commissionedAt` witness for §7's "storage missing but previously commissioned". Atomic temp-plus-`rename` writes; witness writes report whether they landed |
| `registry.ts` / `endpoints.ts` | The live endpoint set and one Matter device-type factory per §4.2 role. Every bridged child publishes the **full** Bridged Device Basic Information identity (`BridgedIdentity`: vendor name/id, product name, hardware + software versions, plus the per-accessory label/serial/uniqueId/reachable) — all optional in the cluster at 0.17.8, all populated, and the same values the root node's `BasicInformation` carries so an ecosystem is never shown two answers |
| `ws-server.ts` | The loopback protocol server (§1–§3). Holds an un-attached socket OPEN while the node is refusing — it is the client's only route to the §3.11 rebuild |
| `protocol.ts` | The wire contract as types + the §1.1 error/refusal domains. No matter.js import, so the protocol is testable without a Matter stack |
| `reconcile.ts` / `window.ts` / `config.ts` | §3.1 planning + arg validation, the enhanced commissioning window, CLI flags |

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
