# `indigo-matter` Implementation Notes

**Status:** Living reference — pruned 2026-06-10
**Companion documents:** [`API.md`](./API.md) (Domio wire contract, authoritative),
[`MATTER.md`](./MATTER.md) (architecture & landscape), [`INSTALL.md`](./INSTALL.md)
(setup),
[`PRD-indigo-matter-plugin.md`](./PRD-indigo-matter-plugin.md) (historical build spec)

This document keeps only what the shipped code and the other docs don't already
say: the **verified matter-server wire protocol** (§1), the **IWS HTTP transport
mechanics** (§2), **alpha-state limitations** (§3), and the **Thread validation
shopping list** (§4). The original pre-build scaffolding (install recipes,
Devices.xml skeletons, a worked cluster-handler example) was removed once the
real code superseded it — the plugin bundle itself is now the reference:
`Contents/Server Plugin/matter_handlers/base.py` defines the `ClusterHandler`
contract (including `node_scoped` fan-out and `on_node_event`), `on_off.py` is
the canonical simple handler, `thermostat.py` the precedent for attribute
writes, and `registry.py` lists everything wired in.

> **Install note (hard-won):** the npm package `matter-server` has **no `bin`
> entry** — `npx matter-server` fails with "could not determine executable to
> run". The managed LaunchAgent therefore launches
> `node …/node_modules/matter-server/dist/esm/MatterServer.js` directly. Full
> install/setup lives in [`INSTALL.md`](./INSTALL.md).

## 1. matter-server WebSocket protocol — VERIFIED

Read directly from the running install on jarvis
(`@matter-server/ws-controller@0.6.2`). The protocol is drop-in compatible with
Home Assistant's python-matter-server. `protocol.py` is the rename firewall —
the **only** module that may know these wire names.

### 1.1 Envelope

- **Request:** `{"message_id": "<unique>", "command": "<name>", "args": {…}}`
- **Success:** `{"message_id": "<same>", "result": …}`
- **Error:** `{"message_id": "<same>", "error_code": <int>, "details": "<text>"}`
  where `error_code` is a `ServerErrorCode` int (0 UnknownError,
  1 NodeCommissionFailed, 2 NodeInterviewFailed, 5 NodeNotExists, …).
- **Event:** `{"event": "<name>", "data": …}` — except `server_info`, which is
  pushed as a **bare object on connect** (no `event`/`message_id` wrapper):
  `{fabric_id, compressed_fabric_id, fabric_index, schema_version, sdk_version,
  bluetooth_enabled, …}` (`sdk_version`, not `version`). Also available as the
  `server_info` command.

### 1.2 Commands the plugin uses (verified arg names)

| Command | Args | Notes |
|---|---|---|
| `server_info` | `{}` | |
| `get_nodes` | `{}` | Returns ALL commissioned nodes regardless of liveness — use each node's `available` flag, not mere presence |
| `get_node` | `{node_id}` | |
| `commission_with_code` | `{code, network_only}` | `network_only: true` in this architecture (share model, no BLE). LAN discovery can exceed 60 s — plugin RPC timeout is 300 s |
| `open_commissioning_window` | `{node_id, timeout}` | Issue #210: `timeout` here is the commissioning-window DURATION in seconds, NOT an RPC deadline — verified against matter-server v1.2.2's `WebSocketControllerHandler.ts`. Omit it and the window defaults to 900 s (matter.js's own default). Result is `{setup_pin_code, setup_manual_code, setup_qr_code}` (snake_case — NOT the camelCase `manualPairingCode`/`qrPairingCode` our own outbound `bridge_protocol` emits, a different wire contract). 1.2.2 accepts `iteration`/`option`/`discriminator` in the arg type but has them commented out of the destructure, so every window opened this way is an Enhanced window. There is no command to ask whether a window is already open, and no way to re-read a code once issued |
| `interview_node` | `{node_id}` | |
| `remove_node` | `{node_id}` | |
| `read_attribute` | `{node_id, attribute_path}` | `attribute_path` is `"endpoint/cluster/attribute"`, decimal |
| `write_attribute` | `{node_id, attribute_path, value}` | Setpoints/modes are **writes**, not commands |
| `device_command` | `{node_id, endpoint_id, cluster_id, command_name, payload}` | + optional `timed_request_timeout_ms`. `command_name` is camelized server-side, so `"On"`/`"Off"`/`"Toggle"` are accepted |
| `start_listening` | `{}` | Returns the **full node dump** as its `result` AND switches on the event firehose — every attribute change then streams as `attribute_updated`, no per-attribute subscription |

### 1.3 Node-details object (from `get_node` / `get_nodes` / `node_added`)

There is **NO `endpoints` key**. A node carries a flat `attributes` map keyed
by `"endpoint/cluster/attribute"` (decimal):

```json
{"node_id": 42, "available": true, "is_bridge": false,
 "attributes": {"0/40/1": "TP-Link", "1/29/0": [{"0": 266, "1": 2}], "1/6/0": false}}
```

Endpoints and their cluster sets are **derived from the attribute paths**
(`matter_model.py` does this); device types come from the Descriptor cluster's
DeviceTypeList (`<ep>/29/0`); BasicInformation (cluster 40) on endpoint 0
gives vendor/product (attrs 1/2/3/4) and SoftwareVersionString (attr 10).
Large ids (node/fabric) are serialised as bare large JSON numbers (Python
parses them as ints natively).

### 1.4 Events the plugin handles (verified `data` shapes)

| Event | `data` shape | Plugin response |
|---|---|---|
| `attribute_updated` | `[node_id, "ep/cl/at", value]` — an **array** | Dispatch to cluster handler → Indigo state (plus node-scoped fan-out, e.g. battery) |
| `node_added` | node-details object | Claim in-flight commission job (authoritative rename/folder) or runtime-create devices |
| `node_updated` | node-details object | Re-sync; apply `available` → reachability |
| `node_removed` | bare `node_id` | Delete/mark Indigo devices |
| `node_event` | node event payload | Generic Switch button presses → Indigo trigger events |
| `endpoint_added` / `endpoint_removed` | `{node_id, endpoint_id}` | Reconcile |
| `server_shutdown` | `{}` | Mark all unreachable; reconnect |

### 1.5 Cluster ID reference (shipped handlers)

As wired in `matter_handlers/registry.py`:

| Cluster | ID (hex) | ID (dec) | Handler |
|---|---|---|---|
| OnOff | 0x0006 | 6 | `on_off.py` |
| LevelControl | 0x0008 | 8 | `level_control.py` |
| ColorControl | 0x0300 | 768 | `color_control.py` |
| PowerSource (battery) | 0x002F | 47 | `power_source.py` (node-scoped fan-out) |
| Switch (Generic Switch / buttons) | 0x003B | 59 | `generic_switch.py` (via `node_event`) |
| BooleanState (contact) | 0x0045 | 69 | `sensors.py` |
| AirQuality | 0x005B | 91 | `air_quality.py` |
| Smoke/CO Alarm | 0x005C | 92 | `smoke_co.py` |
| Valve | 0x0081 | 129 | `valve.py` |
| ElectricalPowerMeasurement | 0x0090 | 144 | `electrical.py` → `curEnergyLevel` (W) |
| ElectricalEnergyMeasurement | 0x0091 | 145 | `electrical.py` → `accumEnergyTotal` (kWh) |
| DoorLock | 0x0101 | 257 | `door_lock.py` |
| WindowCovering | 0x0102 | 258 | `window_covering.py` |
| Thermostat | 0x0201 | 513 | `thermostat.py` |
| FanControl | 0x0202 | 514 | `thermostat.py` merge / primary on fan-only endpoints |
| IlluminanceMeasurement | 0x0400 | 1024 | `sensors.py` |
| TemperatureMeasurement | 0x0402 | 1026 | `sensors.py` |
| PressureMeasurement | 0x0403 | 1027 | `sensors.py` |
| FlowMeasurement | 0x0404 | 1028 | `sensors.py` |
| RelativeHumidityMeasurement | 0x0405 | 1029 | `sensors.py` |
| OccupancySensing | 0x0406 | 1030 | `sensors.py` |
| CO₂ Concentration | 0x040D | 1037 | `air_quality.py` |
| PM2.5 Concentration | 0x042A | 1066 | `air_quality.py` |
| TVOC Concentration | 0x042E | 1070 | `air_quality.py` |
| Descriptor | 0x001D | 29 | (model parsing) |
| BasicInformation | 0x0028 | 40 | (model parsing) |

**Indigo gotcha that bit twice:** Indigo does **not** apply static
`<Supports*>` Devices.xml elements to API-created devices — capability flags
(`SupportsColor*`, `SupportsPowerMeter`/`SupportsEnergyMeter`,
`SupportsBatteryLevel`, `IsLockSubType`) must be set in device **props at
creation**, and are re-asserted at reconcile so capabilities that appear later
(e.g. via device firmware updates) heal existing devices automatically.

## 2. HTTP API — Indigo Web Server (IWS) handlers

The plugin does **not** run its own HTTP server (`aiohttp` is absent from the
Indigo framework Python; IWS is the idiomatic mechanism). The Domio API
([`API.md`](./API.md), authoritative) is served as **hidden-action handlers**:
one `<Action … uiPath="hidden">` per handler in `Actions.xml`, each a method
on the `Plugin` class returning an `indigo.Dict` with
`status` / `headers` / `content`.

```python
def http_commission(self, action, dev=None, caller_waiting_for_result=None):
    props = dict(action.props)
    method = props.get("incoming_request_method", "GET")        # "GET" | "POST"
    path_args = list(props.get("file_path", []))                # GET ONLY — see below
    query = dict(props.get("url_query_args", {}))               # request params
    # ... bridge into the loop: self.runtime.submit(coro).result(timeout) ...
```

**URL shape:** `…/message/com.simons-plugins.indigo-matter/{handler}[/{pathArg}]`,
reachable locally on `:8176` and remotely via the Reflector
(`https://{reflector}.indigodomo.net/message/…`) with the same
`Authorization: Bearer {key}` Domio already uses.

**Authentication:** enforced by Indigo/Reflector **before** the handler runs —
the plugin never sees or validates the credential.

> **IWS platform trap (cost us a live bug):** trailing URL path components are
> delivered in `file_path` **only on GET, never on POST**. POST handlers must
> read identifiers from the query string — hence
> `POST …/decommission?nodeId=0x22`, not `POST …/decommission/0x22`. Path
> form is kept as a GET-era fallback only.

The handlers are thin transport adapters — they bridge into the asyncio loop
via `runtime.submit(coro).result(timeout)` and delegate all logic to
`commission_jobs` and `device_sync`.

## 3. Alpha-state limitations of matter-server (v0.6.2)

- **First-admin Thread commissioning is non-functional** ("For Thread
  Matter.js currently requires a network Name which is not provided" — i.e.
  it cannot supply a Thread operational dataset to a factory-fresh device).
  **This does not block Thread in our architecture:** the share model never
  does first-admin commissioning — Apple Home provisions the device onto
  Apple's Thread mesh, and the plugin joins as a second admin over IP through
  the Apple border router, the same flow validated with Wi-Fi hardware.
  Thread is therefore supported — *validated with a real device* (Aqara
  FP300 via HomePod TBR; see §4 and [`MATTER.md`](./MATTER.md)). Watch
  upstream for first-admin Thread support regardless.
- **BLE commissioning is off by default** (`--ble` to enable). Keep it off:
  the share model never needs it, and on macOS it isn't supported anyway.
- **The server is Alpha**, not CSA-certified. Pin versions; test before
  upgrading. Known soft spot: its availability detection is slow
  (subscription/timeout based) — a dead node can take minutes to flip
  `available: false`.
- **Unauthenticated control socket:** matter-server's WS API has no auth and
  binds all interfaces unless `--listen-address` is given. The managed
  LaunchAgent pins it to `127.0.0.1`.

## 4. Test devices — Thread validation shopping list

Wi-Fi is validated with real hardware (Tapo P110M energy plug, end-to-end via
Domio). These remain useful candidates for the **first real Thread test**
(~£15–£60, UK availability; verify Matter certification on the
[CSA database](https://csa-iot.org/csa-iot_products/) before purchase —
Matter support is sometimes advertised before it ships in firmware):

| Capability | Suggested device | Clusters |
|---|---|---|
| Thread occupancy + lux | Aqara Motion and Light Sensor P2 | OccupancySensing, IlluminanceMeasurement |
| Thread temp + humidity | Aqara Temperature and Humidity Sensor T1 (Matter-over-Thread variant) | TemperatureMeasurement, RelativeHumidityMeasurement |
| Thread contact | Eve Door & Window | BooleanState |

A **mains-powered** Thread device is the gentler first test — sleepy
(battery) devices exercise the alpha controller's weakest paths. Vendor-app
firmware lesson from the Wi-Fi validation: stock firmware may expose fewer
clusters than the hardware supports (the Tapo shipped OnOff-only; energy
clusters arrived via a Tapo-app firmware update) — check
`/diagnostics?nodeId=…` before concluding the plugin missed something.

## 5. Coding conventions

Follow `indigo-domio-plugin` conventions for logging (`self.logger`,
error/warning/info/debug), async (single asyncio loop on a background thread;
never `asyncio.run` from Indigo's main thread), error handling (specific
exceptions, structured error responses, no bare excepts), type hints
(Python 3.11+), and module structure (one cluster handler per file under
`matter_handlers/`).

Tests: pytest, with matter-server mocked at the WebSocket layer
(`tests/fakes.py`) and the `indigo` module mocked. Run from the repo root:
`pytest`.
