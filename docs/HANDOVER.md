# indigo-matter — Build Handover

**Last updated:** 2026-08-03 16:34 UTC
**Branch:** `main` — PRs #106, #107, #108 all merged.
**Version:** `2026.7.13`
**Tests:** 1005 passing (`cd indigo-matter && /Library/Frameworks/Python.framework/Versions/Current/bin/python3 -m pytest -q`)
**Deployed:** jarvis is **unchanged** — still running the #103 code (v2026.7.10 era). **Nothing from #106/#107/#108 has been deployed**, so the #104 supervision fixes are NOT yet live on jarvis.
**Status:** **Wi-Fi AND Thread validated with real hardware.** #104's three server-supervision faults are fixed and merged (#107). #105's diagnostic is merged (#108) but **#105 stays open** — the bridged-endpoint path still has no real-bridge validation. `domio-code` #236 is fixed and closed (domio-code PR #237). Known-open: #105, plus the older #43, #46, #21–#24.

---

## 2026-08-03 session (afternoon) — #105 diagnostic, domio #236, and a live Thread outage

**Merged:** #107 (server supervision, v2026.7.12), #108 (empty-bridge warning, v2026.7.13),
`domio-code` #237 (FabricExists resume). **#104 and `domio-code` #236 closed.**

### #108 — say something when a bridge exposes nothing

Warns when a node produces no devices AND has an Aggregator (`0x000E`) endpoint.
Scoped so a working bridge stays silent and a node that merely creates nothing
isn't blamed on a bridge fault — both limits are tested. **#105 stays open**: its
central claim is that `_bridge_identity` / cluster `0x0039` / dynamic
`endpoint_added` have never met a real bridge, which needs hardware.

> **Trap worth remembering:** the commit said *"Does NOT close #105"* and GitHub
> closed #105 anyway — its linked-issue parser matches the literal `close #105`
> and does not understand negation. Never write the phrase at all; say
> "addresses part of #105" instead. Reopened with the remaining scope restated.

### `domio-code` #236 — the retry that could never match

Two causes, both fixed: `MTRErrorCodeFabricExists` was unhandled, and
`nextNodeID()` handed out a fresh id every attempt, so a half-commissioned device
sat on the fabric under the OLD id forever. `MatterFabricStore` now persists a
per-device node-ID mapping, written **before** the attempt (the case that needs it
is an attempt that fails partway). Keyed on the **setup passcode** — a manual
pairing code truncates the discriminator to its top 4 bits, and QR vs manual
encodings are different strings, so either would miss a user who scans once and
types the next time. Note `MTRErrorCode` does not exist in Swift: `NS_ERROR_ENUM`
imports as `MTRError.Code`.

### Live incident: every Thread device dropped, and it wasn't the plugin

Six devices showed `unreachable` in Indigo. Diagnosis order that worked:

1. **`unreachable` is `errorState`, not a device state.** Querying state
   `reachable: false` returns nothing and looks like all-clear — the plugin sets
   it via `setErrorStateOnServer`. This cost the first wrong answer.
2. All six belonged to **one node**, `0x27` (IKEA ALPSTUGA). matter-server logs
   node ids in **hex**, so its `@1:27` is Indigo address `0x27`.
3. matter-server's err log records **failures only** — silence never proves
   recovery. Confirm from the device's `last_successful_comm`, and check the
   value actually moved (a stale reading looks perfectly plausible).
4. Restarting the HomePod took **all 5 Thread nodes** offline within 3 minutes
   while both Wi-Fi Matter nodes were untouched — proving that one HomePod is the
   **sole Thread border router**, a single point of failure for every Thread
   device. A HomePod update on 08-02 had left it unstable: `0x27` logged 4 of its
   9 all-time "marking unavailable" events that day. The restart fixed it.

**Triage rule:** when Matter devices go unreachable, split them Thread vs Wi-Fi
first. Only-Thread means suspect the border router, not the devices.

The fabric currently holds 8 nodes: `0x27` ALPSTUGA, `0x2d` Aqara FP300, `0x2e`
BILRESA, `0x2f` TIMMERFLOTTE, `0x31` Tapo plug, `0x32` Shelly 1PM, `0x34`
GRILLPLATS, `0x35` **Homebridge iCloud** — the last being #105's empty bridge,
still live and still producing nothing. Decommission when convenient.

### Still not done

- **Deploy to jarvis.** Three merged versions behind; the #104 fixes are not live.
- **#105 real-bridge validation** — needs a Hue bridge or Matterbridge with children.
- **Second Thread border router** — a Thread-capable Apple TV would remove the SPOF.
- CodeRabbit was **rate-limited on every `indigo-matter` PR this session** (#106,
  #107, #108), so all three merged on a passing check that was not a real review.
  `domio-code` #237 did get a genuine review, with no findings.

---

## 2026-08-03 session — closing out #104 (server supervision)

**Shipped:** PR #106 (handover, merged, v2026.7.11). **Open:** PR #107 (commits `cd264c2`,
`2a32910`, `a2d653a`) — all three #104 faults plus the pytest hazard. CI green; CodeRabbit
reported **"Review rate limited"**, so it is a passing check but *not* an actual review.

### The reaper was matching the wrong resource

This is the finding worth carrying forward. The two halves of #104 turned out to be one
root cause: **the reaper matched on storage path, but the resource actually contended is
the port.** A stray from a different install layout held 5580 and kept serving with old
arguments; `_running_server_pids()` required both our package dir *and* our
`--storage-path`, so it was invisible. Reaping on the port would have prevented the whole
misdiagnosis.

The port is now a second, independent match signal. Deliberate limit: a port holder that
cannot be identified as a matter-server is **never signalled**, only warned about with pid
and command. Killing an unrelated listener is a worse failure than the one being fixed.

### Loaded-but-dead needed a distinction the code did not have

`_managed_pid()` collapsed two very different `launchctl print` states into `None`. They
must drive opposite decisions, so `_managed_job()` now separates them:

| `launchctl print` | Meaning | Action |
|---|---|---|
| no `pid =` line at all | loaded but **not running** | revive — bootout + bootstrap |
| `pid =` present, unparseable | may well be alive | hands off (original safety valve) |

**`KeepAlive` was left alone on purpose** (`{SuccessfulExit: false, Crashed: true}`).
Switching to `KeepAlive: true` would fix the respawn but reintroduce the infinite
crash-loop on genuine misconfiguration that this module's docstring exists to prevent.
Detecting and reviving the dead job gets the recovery without that trade.

Third fault: a matching applied-digest proves the right plist was *written*, never that the
live job is using it — launchd caches `ProgramArguments` at bootstrap. Drift is now warned
about, which is the single line that would have made this a one-line diagnosis.

### Two tests were passing for the wrong reason

Worth knowing before trusting this suite's "healthy job" assertions. The base `FakeRunner`
returned empty stdout for `launchctl print`, so "healthy job left untouched" was really the
unparseable-pid safety valve firing — not health. `FakeRunner` now emits a realistic
`pid = N`, so those tests assert what their names claim.

One existing test had to change intent: `test_reload_never_reaps_when_managed_pid_unknown`
pinned the exact behaviour #104 calls a bug (it used "no pid line" to mean "ambiguous").
Split into `..._when_pid_line_is_unparseable` (valve preserved) and
`test_reload_revives_a_job_that_is_loaded_but_dead` (the fix).

New tests were verified against a stashed source tree — 6 of 7 fail without the change; the
7th (`never_reaps_a_foreign_port_holder`) is a regression guard that must keep passing.
Same mutation-testing instinct as the previous session: a test that cannot fail is worthless.

### Still open, and why

- **#105** — the warning for an empty aggregator is an hour's work, but the issue's central
  claim is that the bridged-endpoint path has **never met a real bridge**. That needs a Hue
  bridge or a Matterbridge with real accessories. Closing it on the warning alone would bury
  the thing it was filed to remember. Node `@1:35` still wants decommissioning on jarvis.
- **`domio-code` #236** — unchanged, and the one with real user pain: the device is
  *permanently* un-addable until cleared in iOS Settings. Needs node-ID persistence keyed by
  setup-code/discriminator, since `nextNodeID()` guarantees a retry never matches.

---

## 2026-08-02/03 session — test-net DCL flag, and three operational faults it exposed

**Shipped:** PR #103 (commits `401d996`, `1860abe`, `d0d1c47`), released as v2026.7.10.

### What was added

An off-by-default advanced pref, **Allow test/development device certificates**,
which appends `--enable-test-net-dcl` to the managed matter-server. Devices that
present a test PAA — Homebridge and other dev bridges — otherwise fail device
attestation against the production DCL alone. matter-server 1.2.2 already accepted
the flag; this exposes it. Note it is genuinely additive to production trust, and it
**also** allows test-net OTA firmware to be offered (`MatterController.js:285` →
`allowTestOtaImages: true`), which is why it warns on every start and is documented
as something to untick once the device is paired.

### The bit worth remembering: the code was right on day one

The flag worked from the first deploy. It *looked* broken for about an hour because
of three unrelated operational faults, all now either fixed or filed:

1. **Saving the pref does not apply it.** `closedPrefsConfigUi` writes nothing;
   only `ensure_installed()` regenerates the plist. Fixed for the restart menu
   (it now rebuilds `ServerProcess` from current prefs); the config dialog and docs
   now say reload-or-restart explicitly.
2. **A stray matter-server held port 5580 and kept serving with the old arguments**
   while the plugin logged a healthy `connected … reconciled N nodes`. Every new
   instance died with `EADDRINUSE`. The orphan reaper missed it because
   `_running_server_pids()` matches on *storage path*, and that stray had a different
   one — a server sharing our storage path would have failed earlier, at the storage
   lock. **Filed as #104**, with the suggestion to reap on the contended **port**
   rather than the storage path.
3. **launchd would not respawn it.** matter-server appears to exit 0 on that FATAL,
   and `KeepAlive` is `{SuccessfulExit: false, Crashed: true}`, so a clean exit is
   deliberately not restarted and the job stayed dead. Also in #104.

Diagnosis order that worked, worth reusing: `lsof -nP -iTCP:5580 -sTCP:LISTEN` →
`ps -ww -o pid=,command= -p <PID>` → compare against `ProgramArguments` in the plist.
The log alone cannot distinguish "our server with the new flag" from "someone else's
server on our port", which is why the startup warning is now phrased as what we are
*starting* matter-server with, never as the state of whatever is listening.

### Review rounds (two, both via /pr-review-toolkit:review-pr)

Round one found the flag was invisible in the log and that the restart menu could
report success without applying anything. Round two found my fix for that was itself
unpinned: **mutation testing showed that moving `ensure_installed()` after
`restart()` reintroduces the original bug with the whole suite green.** Fixed by
routing both through one parent mock so the call order is asserted. The same round
found the fix double-restarted (two outages, every CASE session dropped twice) in
exactly the case the menu exists for, which is why `ensure_installed()` now returns
a tri-state (`None` preflight-failed / `True` reloaded / `False` left running).

Also corrected: an `EADDRINUSE` claim I had put in `INSTALL.md` that was wrong in
mechanism (see fault 2 above), and OTA wording that over-claimed relative to the code.

### Field validation

Commissioned a Homebridge Matter child bridge end to end. Same device, same
certificate, opposite outcomes across the fix:

```
20:33:55  Attestation rejected: Device uses a test/development certificate …
20:54:55  Attestation accepted
```

**But no Indigo devices appeared** — the bridge published a bare `Aggregator (0x0e)`
endpoint with no bridged children, so there was nothing to build devices from. The
plugin behaved correctly. The bridged-endpoint path (`_bridge_identity`, cluster
0x0039, dynamic `endpoint_added`) remains **unit-tested only, never validated against
a bridge that actually publishes children**. Filed as **#105**, along with the
suggestion to say something when a commissioned bridge is exposing nothing, and a
note to decommission the now-useless node `@1:35`.

### Related, outside this repo

`domio-code` **#236** — Domio's commissioning dead-ends on
`MTRErrorCodeFabricExists` (code 11). A partly-failed attempt leaves the device on
Domio's fabric, the error is unhandled, and `nextNodeID()` guarantees the retry never
matches the existing node, so the device is permanently un-addable until the user
removes it in iOS Settings ▸ General ▸ Matter Accessories. Suggested fix: catch it and
resume via `openCommissioningWindow` instead of failing.

### Repo hazard worth knowing

`tests/test_plugin_behaviour.py::test_startup_wires_connect_and_disconnect_callbacks`
still constructs a **real** `ServerProcess` against the real `$HOME` and calls
`ensure_installed()`, whose preflight-failure path calls `uninstall()` — which deletes
`~/Library/LaunchAgents/com.simons-plugins.indigo-matter.plist`. On a machine where
the plugin is installed, running `pytest` deletes the live LaunchAgent. The three
menu/startup tests touched by #103 now patch `ServerProcess`; that one does not.
Tracked in #104.

> **Resolved 2026-08-03 in PR #107** — that test now patches `ServerProcess`. A sweep of
> the suite confirmed it was the last one; the hazard described above no longer applies.

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

**Follow-up experiment (same evening, also live on jarvis): #56 fully closed.**
Patched the jarvis copy to set BOTH Supports* False on the button and
air-quality handlers + temporary displayStateId instrumentation. Result:
button `displayStateId` → **`lastButtonEvent`**, air quality → **`airQuality`**.
The precedence rule for API-created devices: a True Supports* prop wins; with
both explicitly False, Indigo falls back to Devices.xml `<UiDisplayStateId>`.
Implemented for real: `display_props = {both False}` on GenericSwitchHandler
and AirQualityHandler (the zoo invariant immediately caught that
generic_switch didn't merge display_props into its spec props — fixed); the
stale-display nag now covers any type disclaiming SupportsOnState; zoo
invariant allows (False, False) only when the XML declares a UiDisplayStateId
pointing at a declared custom state. 747 tests. Full display census of the
16-node fleet (from the instrumentation pass): relays/locks/valves onOffState,
dimmers/covering/fan brightnessLevel, thermostat temperatureInputsAll, value
sensors sensorValue, button lastButtonEvent, AQ airQuality — all correct.

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

**Diagnostics endpoint note:** `GET …/message/com.simons-plugins.indigo-matter/diagnostics?nodeId=0x22` (Bearer = reflector key) returns reachable/vendor/product/softwareVersion + per-endpoint numeric cluster ids — first line of investigation for "why didn't my device get state X".

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
- **jarvis cutover:** `manageLaunchAgent` ON, `nodeBinDir=/Users/simon/.nvm/versions/node/v22.22.3/bin`, listen `127.0.0.1`, port 5580. Plugin writes + bootstraps `~/Library/LaunchAgents/com.simons-plugins.indigo-matter.plist`; run.sh retired. Loopback bind confirmed in matter-server.log; 5 nodes intact; control + restart validated.

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

- **matter-server**: npm pkg `matter-server@0.6.2` at `~/indigo-matter`. **Now launched by the plugin-managed LaunchAgent** (`~/Library/LaunchAgents/com.simons-plugins.indigo-matter.plist`, written by `ServerProcess` since 2026-06-10): `node ~/indigo-matter/node_modules/matter-server/dist/esm/MatterServer.js --port 5580 --listen-address 127.0.0.1 --storage-path ~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server --primary-interface en0`, PATH=`~/.nvm/versions/node/v22.22.3/bin:…`. WS at `ws://127.0.0.1:5580/ws` (loopback-only; BLE off). The old `~/indigo-matter/run.sh` is retired (a backup of its plist is at `~/com.simons-plugins.indigo-matter.runsh.plist.bak-*` for rollback). Logs: `~/Library/Logs/indigo-matter/matter-server.{log,err.log}`.
- **Fabric** (sacred — single point of total loss): `~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server/`. Plugin backups (zip) live in the sibling `…/backups/`. A static safety copy from the migration: `~/indigo-matter-fabric-backup-20260610-124145Z`.
- **Plugin**: installed + running in **Indigo 2025.2**, bundle id `com.simons-plugins.indigo-matter`, **version 2026.1.1**. **`manageLaunchAgent` pref is ON** (`nodeBinDir=~/.nvm/versions/node/v22.22.3/bin`, `matterServerListenAddress=127.0.0.1`, port 5580). Plugin restart regenerates the plist (bootstrap no-ops if matter-server already loaded; running server undisturbed).
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
# then: mcp__indigo__restart_plugin(plugin_id="com.simons-plugins.indigo-matter")
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

## 5. Domio integration (share model) — CONFIRMED this session

Domio no longer commissions; it relays a **share code** (Apple Home is admin 1; the plugin joins as admin 2 over IP).

- **Plugin change made:** `matter_client.commission_with_code` now passes `network_only=true` → IP-only discovery + PASE/CASE, no BLE (verified vs source: `commissionNode` uses `ble: bleEnabled && !onNetworkOnly`). Matter commissioning *adds* a fabric → Apple Home untouched.
- **Contract v1.1 verified against live code:** bundle `com.simons-plugins.indigo-matter`; handlers `status` / `commission` (POST create + GET `commission/{jobId}` poll) / `decommission` / `diagnostics`; params `setupCode/suggestedName/suggestedRoom/discriminator/domioNodeId` (URL query); status enum `pending,commissioning,reading_descriptors,creating_devices,success,failed`; result `{nodeId(hex string),indigoDeviceIds,primaryDeviceId,vendorName,productName,…}`. Auth enforced by Indigo/Reflector before the handler. I do NOT currently read `X-Matter-API-Version` (harmless).
- **Domio contract file:** `domio-code/docs/API.md` (mirror of `indigo-matter/docs/API.md`, v1.1). Their client: `domio-code/.../Services/MatterAPIClient.swift`.

## 6. Architecture quick map (`Contents/Server Plugin/`)

`plugin.py` lifecycle/glue + IWS `http_*` handlers + action bridge · `async_runtime.py` the loop · `protocol.py` **rename firewall** (MatterCommand/MatterWrite/MatterEvent; field names verified vs v0.6.2) · `matter_client.py` reconnecting WS (uses `websockets`) · `commission_jobs.py` job state machine · `http_handlers.py` routing · `device_sync.py` reconcile + priming + state/command/write seams + node_added · `matter_model.py` parse get_node (flat `attributes` map → endpoints) · `server_process.py` launchd PM-B (off by default) · `matter_handlers/` one ClusterHandler per cluster + registry.

**Key invariants:** node-details has NO `endpoints` key (derive from flat `attributes`); `attribute_updated` data is `[node_id,"ep/cl/at",value]`; `node_removed` is a bare id; `server_info` is a bare connect frame (`sdk_version`/`fabric_id`). Setpoints/modes are attribute **writes**, not commands.

## 7. Cross-repo (uncommitted / coordination — left intentionally)
- `domio-code/docs/API.md` (v1.1 contract mirror) — untracked in that repo.
- workspace `docs/plans/PRD-indigo-matter-plugin.md` mirror — untracked.
- ADR Thread caveat + Domio PRD edits — flagged to Simon, not auto-applied (ADR is `accepted`/immutable).
