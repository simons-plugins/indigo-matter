# indigo-matter — Build Handover

**Last updated:** 2026-06-09 21:09 UTC
**Branch:** `feature/m0-m4-validation-loop` — pushed to origin
**Tests:** 164 passing (`cd indigo-matter && /Library/Frameworks/Python.framework/Versions/Current/bin/python3 -m pytest -q`)
**Status:** M0–M8 complete; live-validated on jarvis. Commission half-test, live decommission, and M8 failure-hardening all DONE. Domio share-model contract confirmed. Remaining: Domio E2E (Tapo), version bump + PR.

### 2026-06-09 session — bugs found by live testing + fixed
**Committed `858e35b` (indigo-matter) + `5777ba2` (domio-code):**
1. **suggestedName/suggestedRoom were silently dropped on commission.** `node_added` raced ahead of the commission job. Fix in `device_sync.create_devices`: the commission pass is now *authoritative* — renames + re-folders the existing device. suggestedRoom → an Indigo **device folder** (no native "room"), auto-created. Live-proven.
2. **decommission POST never received its nodeId.** IWS only delivers trailing URL path components on **GET**, never POST. Fix: read `nodeId` from the **query** (`POST …/decommission?nodeId=0xC`), path kept as fallback. Live-proven (real RemoveFabric, idempotent 404). API.md → **v1.2** in both repos; Domio note at `domio-code/docs/matter-api-v1.2-changes.md`.

**This commit (M8 + colour fix):**
3. **M8 failure hardening.** MatterClient gained `on_connect`/`on_disconnect` seams. On drop / `server_shutdown` event → `device_sync.mark_all_unreachable()`. On (re)connect → `_resync` (replaces `_initial_sync`; runs on first connect AND every reconnect) → `reconcile_all`, which now re-primes existing devices + clears stale `unreachable` (`_refresh_live_node`). Sleep/wake is covered by the same machinery (ping_interval=20 drops the stale socket → callbacks fire). Reconnect path live-verified; disconnect/server_shutdown unit-tested only (can't bounce matter-server remotely).
4. **Colour `whiteTemperature` rejected (`invalid color level key`).** Surfaced by M8 re-prime; NOT a stale device (a fresh commission errored identically). Root cause: Indigo does not apply the static `<Supports*>` Devices.xml elements to API-created devices — colour support must be set in device **props at creation**. Fix in `color_control.py`: set `SupportsColor/RGB/White/WhiteTemperature` + `WhiteTemperatureMin/Max` in create props, and guard the `whiteTemperature` write with `if "whiteTemperature" in dev.states`. Live-proven: set 3000K → device received `moveToColorTemperature mireds 333`, Indigo `whiteTemperature` state populated, no error.

**Post-review fixes (from /review-pr, same branch):**
5. **Reachability is now availability-accurate.** Review caught that the M8 reconnect-reconcile cleared `unreachable` for any node merely *present* in the matter-server dump — but `get_nodes` returns all commissioned nodes regardless of liveness. matter-server actually carries an `available` boolean in node-details and fires `node_updated` on availability change. Fix: `NodeInfo.available` parsed from node-details; `_apply_reachability(node)` clears/marks per `available` in both `reconcile_all` and the `node_updated` handler. So a node matter-server reports offline is marked unreachable, and one that recovers is cleared — live, per-device. Plus: `on_connect` task now keeps a strong ref + done-callback (no GC / swallowed exception); `_resync` logs with traceback; fixed a dangling doc-comment ref in color_control. 4 new tests (164 total). NOTE: matter-server v0.6.2's own availability detection is slow (subscription/timeout based) — killing a virtual device did not flip `available` within ~2 min, so the *plugin* handling is unit-tested but live end-to-end availability is gated on matter-server's (alpha) detection.

**Open follow-ups:** (a) diagnostics live response (§3.5) doesn't match the documented shape (numeric cluster ids, no fabrics/network/deviceType) — low priority, future Domio view; (b) Domio's `MatterAPIClient` still needs to implement decommission with `?nodeId=` (guidance shipped in the changes doc); (c) Domio E2E (Tapo); (d) version bump + PR.

This is the resume point. Read this first, then `CLAUDE.md` for architecture and `docs/IMPLEMENTATION.md §2.8` for the verified matter-server protocol.

---

## 1. What's done (milestones)

| Milestone | State | Live-validated on jarvis? |
|---|---|---|
| M0 skeleton + async runtime | ✅ | n/a (unit) |
| M1 protocol adapter + server_process | ✅ | protocol verified vs v0.6.2 source |
| M2 WS client (reconnect, correlation) | ✅ | ✅ (connects, reconciles) |
| M3 commission jobs + IWS HTTP handlers | ✅ | partial — see §4 TODO |
| M4 OnOff → relay | ✅ | ✅ on/off both directions |
| M5 dimmer (LevelControl) | ✅ | ✅ brightness 50/100%, on/off, echo |
| M5 colour (ColorControl) | ✅ | ✅ colour-temperature; RGB Hue/Sat proven plugin-side (virtual device lacked HS feature) |
| M6 sensors (temp/humidity/occupancy/contact/lux) | ✅ | ✅ temperature (real −28 °C); others unit-tested |
| M7 thermostat (+FanControl) | ✅ | ✅ heat setpoint + HVAC-mode writes |

Plus two improvements found by live testing: **state priming** (apply get_node snapshot on create) and **`node_added` runtime creation** (dashboard-commissioned devices appear without restart), and **error self-heal** (errors clear on next command/update success).

## 2. What's LEFT to build

1. ~~**Commission-handler half-test.**~~ ✅ DONE 2026-06-09 (found + fixed the suggestedName/room race — see session note above).
2. **M8 — failure hardening.** Much already exists (reconnect/backoff, error mapping, self-heal, unreachable marking, `_mark_disconnected` fails in-flight requests). ~~Live decommission test~~ ✅ DONE 2026-06-09 (found + fixed the IWS POST path-arg bug). Remaining: handle `server_shutdown` event (mark all unreachable + reconnect), Mac sleep/wake resubscribe.
3. **Domio E2E** (the other agent drives): Apple Home commissions a Tapo P125M → "Turn On Pairing Mode" → share code → Domio `MatterAPIClient` → my `/commission`. I just watch the job succeed + device appear.
4. **Ship:** bump `PluginVersion` in Info.plist (currently `2026.0.1`; bump only when shipping — workspace rule), then open the PR. Don't merge without Simon's go-ahead.
5. Optional v2 clusters: Window Covering, Lock, Smoke/CO, Air Quality, Energy (out of v1 scope per PRD).

## 3. Live environment (jarvis = 192.168.0.41, mounted at `/Volumes/Macintosh HD-1`)

- **matter-server**: `~/indigo-matter` (npm pkg `matter-server@0.6.2`), launched by `~/indigo-matter/run.sh` → `node …/MatterServer.js --storage-path … --primary-interface en0 --listen-address 127.0.0.1`. WS at `ws://localhost:5580/ws` (localhost-only; BLE off). The **ws-controller source is the protocol ground truth**: `~/indigo-matter/node_modules/@matter-server/ws-controller/dist/esm/`.
- **Plugin**: installed + running in **Indigo 2025.2**, bundle id `com.simon.indigo-matter`. `requirements.txt` auto-install of `websockets` worked. `manageLaunchAgent` pref is **off** (connects to the manually-run matter-server).
- **5 commissioned nodes** (Indigo device ids):
  - matterRelay `714038249` (OnOff Light) · matterDimmer `507300015` · matterColorDimmer `200619536` (node `0xF`; recreated 2026-06-09 with the colour-support-props fix, replaces old `747107241`) · matterTemperatureSensor `1824758566` · matterThermostat `1118330069`
- **MCP control**: `mcp__indigo__*` (restart_plugin, query_event_log, get_devices_by_type, get_device_by_id, get_devices_by_state, device_turn_on/off, device_set_brightness, device_set_rgb_color, device_set_white_levels, thermostat_set_heat_setpoint, thermostat_set_hvac_mode). `get_device_by_id` only shows relay/dimmer fields — use `get_devices_by_state {"sensorValue": "<0"}` etc. to read custom states.
- **Cannot exec on jarvis** (no SSH key); CAN read/write its disk via the mount.

### Deploy recipe (update the live plugin)
```
REPO="indigo-matter.indigoPlugin/Contents/Server Plugin"
LIVE="/Volumes/Macintosh HD-1/Library/Application Support/Perceptive Automation/Indigo 2025.2/Plugins/indigo-matter.indigoPlugin/Contents/Server Plugin"
find "$REPO" -name __pycache__ -type d -exec rm -rf {} + ; cp -R "$REPO/." "$LIVE/"
find "$LIVE/.." -name __pycache__ -type d -exec rm -rf {} + ; find "$LIVE/.." -name "*.pyc" -delete
# then: mcp__indigo__restart_plugin(plugin_id="com.simon.indigo-matter")
```
**First-time install only** is double-click; updates are cp + restart. The plugin reconciles on every (re)start and via `node_added`.

## 4. Test rig (this Mac, `/tmp/matter-test`)

`@matter/examples@0.15.6` installed + custom device scripts. **5 virtual devices currently running** (may have died if this Mac slept — re-launch as needed):

| Device | Script / bin | Port | passcode / discriminator | Pairing code |
|---|---|---|---|---|
| OnOff plug | `device-onoff/DeviceNode.js` | 5540 | 20202021 / 3840 | (commissioned) |
| Temp sensor | `device-sensor/SensorDeviceNode.js` `MATTER_TYPE=temperature` | 5541 | 20202023 / 256 | (commissioned) |
| Dimmable | `dimmable-light.mjs` | 5542 | 20202024 / 512 | (commissioned) |
| Colour (ExtendedColorLight) | `color-light.mjs` | 5543 | 20202025 / 768 | (commissioned) |
| Thermostat | `thermostat-device.mjs` | 5544 | 20202026 / 1024 | (commissioned) |

**Hard-won test-rig recipe (these bit us repeatedly):**
- **Distinct UDP port per device** (`MATTER_PORT` / `network.port`). Two devices on 5540 → PASE goes to the wrong socket → "1 discovered, attempt failed".
- **Distinct discriminator with a different TOP nibble** — the 11-digit manual code only carries the 4-bit *short* discriminator. 3840 & 3841 both → short 15. Use 256/512/768/1024 (short 1/2/3/4).
- **Commission promptly** — window is ~15 min, then the device stops advertising.
- **After cycling many device instances, restart matter-server** (Ctrl-C `run.sh`, re-run) to clear its stale mDNS/discovery cache — this fixed two "PASE never reaches the device" failures.
- Custom colour/thermostat devices need their cluster **features** enabled, e.g. `ThermostatDevice.with(ThermostatServer.with("Heating","Cooling"))`, and must NOT set server-managed attrs (currentHue etc.). Scripts in `/tmp/matter-test/*.mjs` already do this.
- `--storage-path` is **ignored** by the matter.js examples; they store under `~/.matter/<id>`.

### Read-only probe (run ON jarvis): `~/matter-probe.js`
Dumps `server_info` + `get_nodes`/`get_node` + toggles. Run: `source ~/.nvm/nvm.sh; nvm use 22 >/dev/null; node ~/matter-probe.js`.

## 5. Domio integration (share model — ADR-0006 C4) — CONFIRMED this session

Domio no longer commissions; it relays a **share code** (Apple Home is admin 1; the plugin joins as admin 2 over IP).

- **Plugin change made:** `matter_client.commission_with_code` now passes `network_only=true` → IP-only discovery + PASE/CASE, no BLE (verified vs source: `commissionNode` uses `ble: bleEnabled && !onNetworkOnly`). Matter commissioning *adds* a fabric → Apple Home untouched.
- **Contract v1.1 verified against live code:** bundle `com.simon.indigo-matter`; handlers `status` / `commission` (POST create + GET `commission/{jobId}` poll) / `decommission` / `diagnostics`; params `setupCode/suggestedName/suggestedRoom/discriminator/domioNodeId` (URL query); status enum `pending,commissioning,reading_descriptors,creating_devices,success,failed`; result `{nodeId(hex string),indigoDeviceIds,primaryDeviceId,vendorName,productName,…}`. Auth enforced by Indigo/Reflector before the handler. I do NOT currently read `X-Matter-API-Version` (harmless).
- **Domio contract file:** `domio-code/docs/API.md` (mirror of `indigo-matter/docs/API.md`, v1.1). Their client: `domio-code/.../Services/MatterAPIClient.swift`.

## 6. Architecture quick map (`Contents/Server Plugin/`)

`plugin.py` lifecycle/glue + IWS `http_*` handlers + action bridge · `async_runtime.py` the loop · `protocol.py` **rename firewall** (MatterCommand/MatterWrite/MatterEvent; field names verified vs v0.6.2) · `matter_client.py` reconnecting WS (uses `websockets`) · `commission_jobs.py` job state machine · `http_handlers.py` routing · `device_sync.py` reconcile + priming + state/command/write seams + node_added · `matter_model.py` parse get_node (flat `attributes` map → endpoints) · `server_process.py` launchd PM-B (off by default) · `matter_handlers/` one ClusterHandler per cluster + registry.

**Key invariants:** node-details has NO `endpoints` key (derive from flat `attributes`); `attribute_updated` data is `[node_id,"ep/cl/at",value]`; `node_removed` is a bare id; `server_info` is a bare connect frame (`sdk_version`/`fabric_id`). Setpoints/modes are attribute **writes**, not commands.

## 7. Cross-repo (uncommitted / coordination — left intentionally)
- `domio-code/docs/API.md` (v1.1 contract mirror) — untracked in that repo.
- workspace `docs/plans/PRD-indigo-matter-plugin.md` mirror — untracked.
- ADR Thread caveat + Domio PRD edits — flagged to Simon, not auto-applied (ADR is `accepted`/immutable).
