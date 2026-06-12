# indigo-matter — Build Handover

**Last updated:** 2026-06-12 20:38 UTC
**Branch:** `fix/device-mapping-hardening` (PR pending)
**Version:** `2026.2.22`
**Tests:** 743 passing (`cd indigo-matter && /Library/Frameworks/Python.framework/Versions/Current/bin/python3 -m pytest -q`)
**Status:** **Wi-Fi AND Thread validated with real hardware.** Tapo P110M (Wi-Fi, energy) + Aqara FP300 (Thread, presence/lux/temp/humidity — tester CliveS). Known-open: issue #56 follow-ups (button + air-quality display states), fan brightness echo (rig flake); issues #43 (bridge-child naming), #46 (fan TurnOn FanMode mapping), #21–#24 (commission hardening).

---

## 2026-06-12 session (late) — endpoint-mapping hardening (issue #58)

Audit prompted by #56 ("what else is in this bug class?"). Three gaps found and
fixed; a **device-zoo contract harness** (`tests/test_device_zoo.py`) now pins
the whole class:

1. **Duplicate actuators.** Deferral chain was only OnOff→Level→Color (+
   Fan→Thermostat). A Matter fan (OnOff mandatory alongside FanControl since
   1.2), a thermostat/lock/valve/covering with OnOff, or a colour light
   *without* LevelControl (zoo caught that one immediately) all produced a
   duplicate relay/dimmer. Fix: `ENDPOINT_OWNER_CLUSTERS` in
   `matter_handlers/base.py` (Valve 0x0081, DoorLock 0x0101, WindowCovering
   0x0102, Thermostat 0x0201, FanControl 0x0202); OnOff and LevelControl defer
   to any of them, and OnOff also defers directly to ColorControl.
2. **Unknown devices vanished.** An endpoint with only unhandled clusters
   produced nothing; a node of such endpoints commissioned to SUCCESS with
   `indigoDeviceIds: []` (Domio shows success, Indigo shows nothing). Fix:
   `matterUnknown` placeholder (its `supportedClusters` ConfigUI field was
   already in Devices.xml, never wired) for device-bearing endpoints ≥1 whose
   clusters aren't all in `_NON_DEVICE_CLUSTERS`; INFO log invites a GitHub
   device-support report. When a later pass finds the endpoint supported
   (firmware update — the Tapo pattern), the real device is created and an
   INFO says the placeholder can be deleted (never auto-deleted).
3. **Type-edit guard.** Indigo's Edit Device dialog always offers the full
   Type menu (platform behaviour, can't be removed). `createdTypeId` is now
   stamped into props at creation and healed onto legacy devices by the
   reconcile self-heal; `validateDeviceConfigUi` rejects a type change with an
   explanatory alert; `deviceStartComm` warns as backstop for edits made while
   the plugin was off.

The zoo asserts, over every cluster-combo entry: expected device types, at
most one actuator per endpoint, spec types exist in Devices.xml, initial
states declared, sensor specs carry explicit display props (the #56 class).
**Add new wild devices' cluster sets to ZOO** — invariants run automatically.

**Live-rig verification (same evening, jarvis on the 2026.2.22 branch build):**
deployed via rsync + restart with all 15 virtual devices running. First
reconcile healed every pre-fix device in one pass — 9 value sensors + smoke
got display props, all 20 devices got `createdTypeId`, every replace passed
the persistence verification (zero "did not persist" warnings). Second
restart: "reconciled 16 Matter node(s)" with NO re-asserts and **NO
stale-display warnings** → **real Indigo DOES re-derive `displayStateId` from
`replacePluginPropsOnServer`**. Confirmed on-device: the temp sensor's
`on_state` went `false` → `null` (built-in onOffState removed). So the #56 fix
is fully self-healing — users (incl. CliveS) just upgrade; delete-and-recreate
is never needed and the stale-display warning is pure dead-man insurance.

This finding also unlocks the #56 follow-ups: since display re-derives from a
props replace, the button (`lastButtonEvent`) and air-quality (`airQuality`
string) displays can be experimented with live by toggling Supports* props —
no recommission needed. Still untested: what displayStateId becomes when BOTH
Supports* are False (does Devices.xml UiDisplayStateId finally apply?).

---

## 2026-06-12 session — THREAD VALIDATED + sensor display fix (issue #56)

**Thread is real-device validated.** Tester CliveS commissioned an **Aqara FP300**
presence multi-sensor (Matter over Thread) via the share model: Apple Home admin 1,
HomePod did the Thread provisioning, manual setup-code menu, **~10 s join** (vs the
Tapo's 124 s). All four endpoints became Indigo devices; node-scoped battery fan-out
and unprompted live attribute reports both worked. Environment: Indigo 2025.2, plugin
2026.2.20, **matter-server 0.6.8** (the INSTALL.md `^0.6.2` caret install floats
within 0.6.x — 0.6.8 behaved), Node 22.22.2, managed LaunchAgent.

**The bug the test found (issue #56, fixed this session):** value sensors displayed
"off" instead of the reading — the sensor family had slipped through the colour
lesson (2026-06-09 item 4): `Supports*` must be **creation props**; Devices.xml
statics (including `<UiDisplayStateId>`) don't apply to API-created devices. Indigo
defaulted `SupportsOnState=True`/`SupportsSensorValue=False` and derived
`displayStateId=onOffState`; the populated `sensorValue` was the XML-declared custom
state shadowing the disabled built-in. Fix: `display_props` class attr on handlers
(value sensors `SupportsSensorValue=True, SupportsOnState=False`; binary sensors the
inverse, explicit), merged into creation props AND re-asserted exactly (not add-only —
absence of `SupportsOnState` means Indigo's default True) by the #45 reconcile
self-heal. Devices created pre-fix may keep their cached `displayStateId` even after
the props heal — a reconcile-time warning names each such device with the remedy:
**delete the Indigo device and reload the plugin; reconcile recreates it correctly**
(no decommission needed).

**Open follow-ups from #56:** `matterButton` (`lastButtonEvent`) and the air-quality
string display (`airQuality`) both rely on `UiDisplayStateId`, which API-created
devices ignore — needs hardware testing to find a working mechanism.

---

## 2026-06-10 session (evening) — FIRST REAL DEVICE: Tapo P110M end-to-end via Domio

**Result: complete success.** Real Matter Wi-Fi energy plug (Tapo P110M, vendorId 5010, productId 264) commissioned through the production path and live in Indigo as `Study matter plug` (device `678761951`, node `0x22`, matterRelay + energy states).

**The flow that worked (= the Domio E2E milestone, now DONE):**
1. Apple Home commissioned the plug (admin 1) — **failed on the main SSID** (advanced Wi-Fi features), succeeded on the backward-compatible **IoT SSID** (same 192.168.0.x subnet as jarvis — that's the requirement; mDNS + link-local IPv6 don't cross subnets).
2. Apple Home → Turn On Pairing Mode → share code → **Domio "Add Matter device"** → plugin `/commission` → joined as admin 2 over IP (`network_only`). Job → `node_added` → device created, on/off both directions working.

**Energy-path findings (record for all Tapo M-series):**
- **Stock P110M firmware 1.0.0 exposes OnOff only** over Matter (ep1 clusters 3,4,5,6,29 — verified via the live `/diagnostics?nodeId=0x22` endpoint, which earned its keep today). No 0x0090/0x0091 → plugin correctly created a plain relay. Energy clusters are Matter 1.3, added by TP-Link **firmware update via the Tapo app**.
- Adding to the Tapo app required the 5s button hold (Wi-Fi reset, *keeps* fabrics; 10s = factory reset, wipes fabrics — warn users) + QR scan; the Tapo app has no "enter Matter code" path.
- After firmware update + node rejoin, **the existing Indigo device gained `curEnergyLevel`/`accumEnergyTotal` automatically — no restart, no recreate**. That is PR #48 (capability-props re-assertion at reconcile) validated on real hardware, exactly its design scenario.
- Fabric survived Wi-Fi reset + Tapo onboarding + firmware update; plug is now 4-admin (Apple Home, Indigo, Tapo cloud, Home Assistant) with no cross-interference. Apple Home took ~a minute to recover post-update — normal settling, not a fault.

**Diagnostics endpoint note:** `GET …/message/com.simon.indigo-matter/diagnostics?nodeId=0x22` (Bearer = reflector key) returns reachable/vendor/product/softwareVersion + per-endpoint numeric cluster ids — first line of investigation for "why didn't my device get state X".

---

## 2026-06-10 session (later) — device-class milestone Wave 1 (multi-agent build)

Nine parallel Sonnet subagents (Fable coordinating, one GitHub issue each, isolated worktrees) built, cross-reviewed, and merged PRs #32–#40 in pre-assigned version order **2026.2.0 → 2026.2.8** (releases auto-created per merge). 261 → **490 tests**.

**New cluster handlers** (all per the handler-registry pattern): `power_source.py` (0x002F → batteryLevel, **node-scoped fan-out** — new `ClusterHandler.node_scoped` flag + fan-out in `device_sync._on_attribute`/`_prime_states`; `SupportsBatteryLevel` prop stamped centrally in `create_devices`), `window_covering.py` (0x0102 → dimmer, lift % inverted+clamped, 100 = open), `door_lock.py` (0x0101 → relay, **`IsLockSubType` prop** so Indigo shows lock UI; handles both `Lock`/`Unlock` and `TurnOn`/`TurnOff` enums), `valve.py` (0x0081 → relay; Toggle refuses to guess direction on unknown state — flood safety), `smoke_co.py` (0x005C → sensor, nulls never read as "Normal"), `air_quality.py` (0x005B + CO2/PM2.5/TVOC, additive sensors), `electrical.py` (0x0090/0x0091 → non-primary merge; `SupportsPowerMeter/EnergyMeter` props injected at creation in on_off/level_control/**color_control**), sensors.py gained Pressure (0x0403) + Flow (0x0404); `FanControlHandler` is now primary on thermostat-less endpoints → `matterFan` dimmer (fan-only endpoints previously created NO device).

**Review-pass catches worth remembering:** lock devices need the `IsLockSubType` creation prop (Devices.xml statics don't apply to API-created devices — colour lesson again); node-scoped fan-out needed per-device exception isolation (deleted device mid-loop aborted the rest); colour bulbs with energy clusters would have silently dropped power data; valve Toggle's `getattr(..., False)` default would have sent Open to an unknown-state water valve.

**Left open:** (a) **deploy 2026.2.8 to jarvis** (rsync recipe in §3) + restart + verify `reconciled 5 Matter node(s)`; existing live devices predate the new creation-props (SupportsBatteryLevel/energy) and won't show those UIs until recreated; (b) #4 Generic Switch PR (in flight — adds `node_event` parsing in protocol.py, `ClusterHandler.on_node_event` hook, `matterButton` device); (c) #7 bridges after #4; (d) live validation of all new classes needs real devices (none owned yet for most).

---

## 2026-06-10 session — managed matter-server, commission hardening, backup/restore

All merged to `main`, all deployed to jarvis. Plugin **2026.0.1 → 2026.1.1** over the session.

**Commission hardening (PRs #2, #20 → 2026.0.3):**
- **Cancellation no longer strands a job** (asyncio.CancelledError caught → job FAILED, setup code freed). **503 precheck** when matter-server is down. **Status 503 body** now carries the error envelope (API.md §3.1).
- **Commission timeout race fixed.** matter-server discovery can take >60s (observed ~124s on the LAN); the old 60s RPC timeout falsely reported `failed` while the device silently joined. Now: RPC timeout 300s + a **reconcile** path — a late `node_added` claims the timed-out job, applies suggestedName/Room, flips it to `success`. Timeout maps to `commissioning_timeout` (was empty `internal_error`). nodeId now stamped on the Indigo device **address**; INFO log on device creation. API.md → **v1.3** (`primaryDeviceId` nullable for unsupported-cluster joins; mirrored to domio-code, PR #198 there).
- Hardening follow-ups filed: **#21** (retry-interleaving remove_node hazard), **#22** (recency-only reconcile match), **#23** (late responses dropped unlogged), **#24** (half-open state cleanup).

**Managed matter-server LaunchAgent (PRs #25, #27, #28 → 2026.0.6) — jarvis migrated off run.sh:**
- **#25 nvm support:** `nodeBinDir` pref + auto-detect (nvm default alias / newest, then Homebrew). The old code only probed Homebrew npx.
- **#27 loopback security:** `matterServerListenAddress` pref, default `127.0.0.1`. matter-server's WS control API is **unauthenticated** and binds **all interfaces** without `--listen-address` — the managed agent now restricts to loopback.
- **#28 node-direct launch:** the `matter-server` npm package has **no bin** (`"bin": null`), so `npx matter-server` fails ("could not determine executable to run"). The managed agent now runs `node …/node_modules/matter-server/dist/esm/MatterServer.js` directly (reads `main` from the package).
- **jarvis cutover:** `manageLaunchAgent` ON, `nodeBinDir=/Users/simon/.nvm/versions/node/v22.22.3/bin`, listen `127.0.0.1`, port 5580. Plugin writes + bootstraps `~/Library/LaunchAgents/com.simon.indigo-matter.plist`; run.sh retired. Loopback bind confirmed in matter-server.log; 5 nodes intact; control + restart validated.

**Docs (PR #29 → 2026.0.7):** `docs/INSTALL.md` (full matter-server install/setup guide), tidied README (was v1.1/M0–M4), **MIT `LICENSE`** (© 2026 Simon Clark).

**Fabric backup & restore (PRs #30, #31 → 2026.1.0/2026.1.1) — closes #26, live-validated:**
- `fabric_backup.py` (pure/injectable). **Backup** zips the sacred storage dir to a sibling `backups/fabric-<UTC>.zip` (atomic .tmp→replace, validated, pruned to 10). **Restore** (managed-mode only, confirm-gated): validate → `stop()` → **move live fabric aside (never delete in place)** → extract → `start()`, with **full rollback** on any failure and a **zip-slip guard**. `ServerProcess.stop()/start()` added.
- Two `/review-pr` passes found + fixed real safety holes (ignored stop/start bools → success-over-dead-server; swallowed rollback; unvalidated backup output; empty-archive wipe). **#31** fixed a live-caught menu bug: Indigo passes ConfigUI menu callbacks `(self, valuesDict, menuId)` — handlers were 2-arg.
- **Live on jarvis:** Export → valid 915-member zip; Restore → server stop → swap (original → `matter-server.pre-restore-…`) → start → reconnect → **5 nodes reconciled** → device toggles. The two `Matter Warning: connection lost` lines during restore are the **expected ~6s reconnect window**, not errors.

**Known polish (not filed):** during an *intentional* matter-server restart (restore / "Restart matter-server"), the reconnect loop logs `connection lost` WARNINGs for ~6s. Consider debouncing — log first attempts at debug, escalate to WARNING only past the normal restart window (`matter_client.py`). Small change + test.

**Next milestone — device-class gaps:** umbrella **#15**; priority order #3 Power Source/battery (promised in PRD, never wired), #4 Generic Switch (buttons — needs `node_event` plumbing through protocol.py + device_sync), #5 Window Covering, #6 Door Lock, #7 bridges-done-properly, #8 standalone fans (a fan-only endpoint currently creates NO device), #9 energy, #10 smoke/CO, #11 air quality, #12 valve, #13 pressure/flow, #14 RVC (icebox). All follow the handler-registry pattern (one `matter_handlers/*.py` + `Devices.xml` type + one line in `registry.default_handlers()`; `thermostat.py` is the precedent for attribute writes).

---

## (historical) 2026-06-09 session — M0–M8 + live validation

### 2026-06-09 session — bugs found by live testing + fixed
**Committed `858e35b` (indigo-matter) + `5777ba2` (domio-code):**
1. **suggestedName/suggestedRoom were silently dropped on commission.** `node_added` raced ahead of the commission job. Fix in `device_sync.create_devices`: the commission pass is now *authoritative* — renames + re-folders the existing device. suggestedRoom → an Indigo **device folder** (no native "room"), auto-created. Live-proven.
2. **decommission POST never received its nodeId.** IWS only delivers trailing URL path components on **GET**, never POST. Fix: read `nodeId` from the **query** (`POST …/decommission?nodeId=0xC`), path kept as fallback. Live-proven (real RemoveFabric, idempotent 404). API.md → **v1.2** in both repos; Domio note at `domio-code/docs/matter-api-v1.2-changes.md`.

**This commit (M8 + colour fix):**
3. **M8 failure hardening.** MatterClient gained `on_connect`/`on_disconnect` seams. On drop / `server_shutdown` event → `device_sync.mark_all_unreachable()`. On (re)connect → `_resync` (replaces `_initial_sync`; runs on first connect AND every reconnect) → `reconcile_all`, which now re-primes existing devices + clears stale `unreachable` (`_refresh_live_node`). Sleep/wake is covered by the same machinery (ping_interval=20 drops the stale socket → callbacks fire). Reconnect path live-verified; disconnect/server_shutdown unit-tested only (can't bounce matter-server remotely).
4. **Colour `whiteTemperature` rejected (`invalid color level key`).** Surfaced by M8 re-prime; NOT a stale device (a fresh commission errored identically). Root cause: Indigo does not apply the static `<Supports*>` Devices.xml elements to API-created devices — colour support must be set in device **props at creation**. Fix in `color_control.py`: set `SupportsColor/RGB/White/WhiteTemperature` + `WhiteTemperatureMin/Max` in create props, and guard the `whiteTemperature` write with `if "whiteTemperature" in dev.states`. Live-proven: set 3000K → device received `moveToColorTemperature mireds 333`, Indigo `whiteTemperature` state populated, no error.

**Post-review fixes (from /review-pr, same branch):**
5. **Reachability is now availability-accurate.** Review caught that the M8 reconnect-reconcile cleared `unreachable` for any node merely *present* in the matter-server dump — but `get_nodes` returns all commissioned nodes regardless of liveness. matter-server actually carries an `available` boolean in node-details and fires `node_updated` on availability change. Fix: `NodeInfo.available` parsed from node-details; `_apply_reachability(node)` clears/marks per `available` in both `reconcile_all` and the `node_updated` handler. So a node matter-server reports offline is marked unreachable, and one that recovers is cleared — live, per-device. Plus: `on_connect` task now keeps a strong ref + done-callback (no GC / swallowed exception); `_resync` logs with traceback; fixed a dangling doc-comment ref in color_control. 4 new tests (164 total). NOTE: matter-server v0.6.2's own availability detection is slow (subscription/timeout based) — killing a virtual device did not flip `available` within ~2 min, so the *plugin* handling is unit-tested but live end-to-end availability is gated on matter-server's (alpha) detection.
6. **Review test-gaps closed.** Added the green tests from the review: multi-endpoint authoritative rename after a node_added race, folder-resolution-failure → folder 0, existing-room-folder reuse, on_connect re-fires on reconnect, `_on_disconnected`→mark_all_unreachable, `_resync`-failure-doesn't-reconcile, and startup wires on_connect/on_disconnect/on_event. **171 tests total.**

**PR #1 full-plugin review (second /review-pr, on the open PR — covered the M0–M7 core):**
7. **Core fixes (red/yellow/green all done).** (a) `commission_jobs.create_job` no longer strands a PENDING job + locks the setup code if the loop is down → returns 503 and rolls back; (b) `device_sync._on_attribute` now guards the handler transform so a malformed value can't silently freeze a device (logs instead), + malformed-event logging + priming-log parity; (c) commissioning rejections (`ProtocolError`) map to `commissioning_failed` (+`matterErrorCode`), not `internal_error`; (d) type tightening (`Job.status: JobStatus`, `NodeInfo.attributes` typed); (e) `async_runtime.stop()` keeps refs on join-timeout; (f) run-loop future + restart() failures now surfaced (done-callback, throttled health warning, `restart()→bool`). 17 new tests. **188 tests total**, deployed + reconcile re-verified on jarvis.

**Open follow-ups:** (a) diagnostics live response (§3.5) doesn't match the documented shape (numeric cluster ids, no fabrics/network/deviceType) — low priority, future Domio view; (b) Domio's `MatterAPIClient` still needs to implement decommission with `?nodeId=` (guidance shipped in the changes doc); (c) Domio E2E (Tapo); (d) version bump + PR.

This is the resume point. Read this first, then `CLAUDE.md` for architecture and `docs/IMPLEMENTATION.md §1` for the verified matter-server protocol.

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
3. ~~**Domio E2E**~~ ✅ DONE 2026-06-10 with a real Tapo P110M (see session note at top): Apple Home → pairing mode → share code → Domio → `/commission` → device live with energy states.
4. **Ship:** bump `PluginVersion` in Info.plist (currently `2026.0.1`; bump only when shipping — workspace rule), then open the PR. Don't merge without Simon's go-ahead.
5. Optional v2 clusters: Window Covering, Lock, Smoke/CO, Air Quality, Energy (out of v1 scope per PRD).

## 3. Live environment (jarvis = 192.168.0.41, mounted at `/Volumes/Macintosh HD-1`)

- **matter-server**: npm pkg `matter-server@0.6.2` at `~/indigo-matter`. **Now launched by the plugin-managed LaunchAgent** (`~/Library/LaunchAgents/com.simon.indigo-matter.plist`, written by `ServerProcess` since 2026-06-10): `node ~/indigo-matter/node_modules/matter-server/dist/esm/MatterServer.js --port 5580 --listen-address 127.0.0.1 --storage-path ~/Library/Application Support/com.simon.indigo-matter/matter-server --primary-interface en0`, PATH=`~/.nvm/versions/node/v22.22.3/bin:…`. WS at `ws://127.0.0.1:5580/ws` (loopback-only; BLE off). The old `~/indigo-matter/run.sh` is retired (a backup of its plist is at `~/com.simon.indigo-matter.runsh.plist.bak-*` for rollback). Logs: `~/Library/Logs/indigo-matter/matter-server.{log,err.log}`.
- **Fabric** (sacred — single point of total loss): `~/Library/Application Support/com.simon.indigo-matter/matter-server/`. Plugin backups (zip) live in the sibling `…/backups/`. A static safety copy from the migration: `~/indigo-matter-fabric-backup-20260610-124145Z`.
- **Plugin**: installed + running in **Indigo 2025.2**, bundle id `com.simon.indigo-matter`, **version 2026.1.1**. **`manageLaunchAgent` pref is ON** (`nodeBinDir=~/.nvm/versions/node/v22.22.3/bin`, `matterServerListenAddress=127.0.0.1`, port 5580). Plugin restart regenerates the plist (bootstrap no-ops if matter-server already loaded; running server undisturbed).
- **5 commissioned nodes** (Indigo device ids):
  - matterRelay `714038249` (OnOff Light) · matterDimmer `507300015` · matterColorDimmer `200619536` (node `0xF`; recreated 2026-06-09 with the colour-support-props fix, replaces old `747107241`) · matterTemperatureSensor `1824758566` · matterThermostat `1118330069`
- **MCP control**: `mcp__indigo__*` (restart_plugin, query_event_log, get_devices_by_type, get_device_by_id, get_devices_by_state, device_turn_on/off, device_set_brightness, device_set_rgb_color, device_set_white_levels, thermostat_set_heat_setpoint, thermostat_set_hvac_mode). `get_device_by_id` only shows relay/dimmer fields — use `get_devices_by_state {"sensorValue": "<0"}` etc. to read custom states.
- **Cannot exec on jarvis** (no SSH key); CAN read/write its disk via the mount.

### Deploy recipe (update the live plugin)
```
SRC="indigo-matter.indigoPlugin/Contents"
DST="/Volumes/Macintosh HD-1/Library/Application Support/Perceptive Automation/Indigo 2025.2/Plugins/indigo-matter.indigoPlugin/Contents"
rsync -rc --exclude='__pycache__' --exclude='*.pyc' "$SRC/Server Plugin/" "$DST/Server Plugin/"
rsync -c "$SRC/Info.plist" "$DST/Info.plist"
# verify: md5 -q "$SRC/Server Plugin/plugin.py" vs "$DST/Server Plugin/plugin.py"
# then: mcp__indigo__restart_plugin(plugin_id="com.simon.indigo-matter")
```
**First-time install only** is double-click; updates are rsync + restart. The plugin reconciles on every (re)start and via `node_added`. **Verify health** after restart via `mcp__indigo__query_event_log` → expect `connected to matter-server, listening` + `reconciled 5 Matter node(s)`. **Cannot exec on jarvis** (SSH to the prod host is blocked) — read/write its disk via the mount; trigger plugin **menu actions** (backup/restore) only via the Indigo UI (no MCP hook).

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
