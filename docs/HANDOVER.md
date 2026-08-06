# indigo-matter — Build Handover

**Last updated:** 2026-08-06 13:35 UTC
**Active work:** none — nothing in flight, working tree clean.
**Branch:** `main` — **PR #142 MERGED** (`3e1229d`, merge commit, `[no-release]`
so **no GitHub release was cut** — Simon is cutting that himself). Latest
release remains v2026.7.23.
**Version:** plugin `2026.8.4`, bridge-node `0.7.0` (**published to npm**;
`DEFAULT_INSTALL_SPEC` pins it).
**Tests:** **2252 Python**, **383 TS** — both green.
(`python3 -m pytest -q` · `cd bridge-node && npm run build && npm test`)
**Deployed:** jarvis runs plugin `2026.8.4` + bridge-node `0.7.0`, paired to
**Apple Home AND Alexa simultaneously** (3 fabrics: 2 Apple, 1 Alexa).
**Status:** export v1 feature-complete and live. #141 **fixed and confirmed**
(below). **#143 is the open one** — see the 2026-08-06 §#143 section; its
defect B was investigated hard today and the leading theory was **withdrawn**,
so read that before touching it.

**NEXT UP (Simon, 2026-08-06):** ~~tidy `MenuItems.xml` into groups~~ — **done**,
see the §#134 section immediately below. The constraint it warned about was
wrong in the useful direction: **separators do exist**.

---

## 2026-08-06 — issue #134: the menu, and the separator the docs deny

Plugin `2026.8.5`. Suites: **2256 Python** (from 2252), pylint **9.42**
unchanged. Branch `feat/134-menu-sections`. TS untouched.

**An empty, self-closing `<MenuItem id="…"/>` renders as a menu separator.**
The canonical `MenuItems.xml` reference lists `<Name>` as *required* and
documents neither separators nor submenus, which is why the note this replaces
said grouping could only be ordering plus name prefixes. It is wrong — or at
least incomplete. Perceptive Automation's own bundled plugins use the form
(**Timers and Pesters**, **Timed Devices**, **Virtual Devices**, **Alexa**),
`Actions.xml` takes it too (`<Action id="sep1" uiPath="DeviceActions"/>`), and
**15 of the plugins installed on jarvis** rely on it. Simon knew from the
Timers and Pesters *action* list that sections were possible; the docs alone
would never have said so.

Worth generalising: the canonical docs are the authority on what is
*supported*, not on what *works*. When a UI affordance is missing from them,
grep the installed plugins before concluding it does not exist —
`grep -rlE "<MenuItem[^>]*/>" …/Plugins/*/Contents/Server\ Plugin/MenuItems.xml`
answered this in one command.

**Five sections**, everyday first, destructive last: devices Indigo controls ·
devices Indigo publishes · matter-server plumbing · export-bridge plumbing ·
backup and recovery. The two `Install/update` items — #134's actual complaint,
clicked for one another live — now sit in **different** sections rather than
merely adjacent, because they are separately versioned, separately installed
npm packages.

**`Export fabric backup…` → `Back up the Matter fabric…`** (and `Restore
fabric backup…` → `Restore a fabric backup…`). "Export" meant save-to-disk in
one item and the outbound bridge in eight others. Menu **ids and callback
methods are unchanged**, so nothing in `plugin.py`, `bridge_agent.py` or
`server_process.py` moved; `spec.install_menu_name` still matches a real
`<Name>`, which `test_bridge_agent.py` pins.

**The tests assert the ORDER**, for the same reason #141's do: a membership
check passes just as happily with all sixteen items back in one flat list.
Mutation-verified both ways — deleting the four separators, and deleting only
`sepServerFromBridge` so the two install sections merge, each fail exactly the
three layout tests and nothing else.

**Not done here, deliberately:** #132 (the Rebuild Endpoint Map warning
overstates what the action does) is dialog *text* in the same file and a
separate bug — reordering it and rewriting it in one commit would have made
the diff unreviewable. #131 (sort exported devices to the top of the picker)
is untouched.

**Stale, spotted in passing, not fixed:** `docs/INSTALL.md` §Overview still
says the bridge package "is not on the npm registry yet, so this half cannot
be installed today". `indigo-matter-bridge@0.7.0` is published. That sentence
belongs to the E8 docs pass, along with #137/#138.

---

## 2026-08-06 (late) — issue #143: state wrong in Alexa. READ THIS BEFORE RESUMING IT

**Status: unresolved, parked for beta testers. The node is EXONERATED.**

Two halves, one root shape — we lean on matter.js's **attribute-change**
machinery for things that are not changes:

* **Defect A (inbound), still the substantive half.** Ecosystem commands are
  emitted from `onOff$Changed` listeners, so an invoke matching the endpoint's
  current state changes nothing and reaches Indigo never. Suspected, **not
  proven**. Untouched by today's work.
* **Defect B (outbound).** New exports show stale, then *unresponsive*, in
  **Alexa only** (Apple is fine) — then **come right on their own after minutes
  with no intervention**. Best current reading: **Alexa-side convergence lag**,
  probably not our bug.

**Do NOT re-assert the "born-vs-changed" theory as proven.** It is real in code
(`createEndpoint` bakes spec states into `initialState`, so an attribute born
`true` emits no change report) and the fix is cheap — construct from role
defaults, then `applyStates` after `aggregator.add()`, exactly as `update()`
already does. But it does **not** explain what was observed: a genuine write
also failed to move Alexa, and Alexa then fixed itself with no write at all.
I posted a "B2 CONFIRMED" verdict on the issue and then **withdrew it**; both
comments are there, read to the end.

**Measured, so don't re-measure it:** a controlled toggle produced `I/ReportData`
on all three live sessions (1 Apple, 2 Alexa Echo hubs), each acked in ~15ms.
Alexa = fabric index 4, **two wildcard subscriptions**, maxInterval ≈188s.
Outbound reporting to Alexa works, *including* for endpoints added after the
subscription existed.

**Three blind alleys — do not re-walk:**
1. matter.js does **not** flush a constructor-supplied `initialState` to its
   store, so `…/indigo-matter-bridge/root.parts.aggregator.parts.<id>.onOff.onOff`
   is **NOT** a reading of the live attribute. Two said `false` for accessories
   that were on and correct in both apps. Cost about an hour.
2. "the node resurrects a stale persisted value on re-export" — tested three
   ways (in-process, across a restart, via `upsert_endpoint`); **all pass**.
   Those tests are committed (`e22e861`, `bridge-node/test/restore.test.ts`) and
   they are what exonerate the node. The stored value was always right; the bug
   was an *unreported* one, which no assertion on `stateOf` can catch.
3. **Never credit the last thing you did.** Something here fixes itself on its
   own timescale. "The plugin restart fixed it" was claimed twice and was wrong
   both times. Any intervention needs a control before causation is claimed.

**To settle it** (needs beta testers, more than one Alexa setup): export a
device that is **on**; record Alexa at t+0/+1/+5/+15 min **without touching
anything**; watch `root.subscriptions.subscriptions` for a re-subscribe at the
moment it comes right. Then repeat with the create-path fix applied — if Alexa
is right at t+0 the fix matters, if the curve is unchanged it is cosmetic.

**Also open, adjacent:** #140 — the drift line
(`indigo-459564566 expected 5, got 2`) now fires on **every** attach after the
factory reset, and `endpoint-map.json` records number `5` twice (Hallway Lamp
and Georges Pendant). Harmless — live numbers are unique — but it is the reset's
fingerprint and belongs in #140.

---

## 2026-08-06 — issue #141: the bridge is never online with an empty accessory list

Plugin `2026.8.4`, bridge-node `0.6.0`. Suites: **2243 Python**, **366 TS**
(from 2243/347 — `npm test` measured 347 on this branch's base, not the 348 the
E6/E7 section records; the note there about measuring counts applies to itself).
pylint 9.42.

**The defect, measured on jarvis after a reboot rather than inferred.** The node
called `server.start()` with an aggregator that had zero children and stayed
empty until the plugin connected and attached — 23 seconds. Apple reconnects
inside that window, reads an empty `PartsList`, concludes every accessory has
gone, and when they reappear treats them as NEW accessories: dumped in the
bridge's own room, with metadata the user can no longer edit. Every restart of
the node — reboot, plugin upgrade, agent restart, crash recovery — therefore
destroyed the user's room assignments for every exported device, and the only
remedy was a full unpair/re-pair (#139).

**The fix.** `endpoint-map.json` gains a schema version 2 in which each entry
carries `role` and `label` alongside `number`, and `BridgeNode.start()` rebuilds
every restorable entry — ordinary `createEndpoint`, same `Endpoint.id`, same
persisted number, `reachable: false`, role-default state — **before**
`server.start()`. `attach` stays authoritative and reconciles against the
restored set exactly as before.

Four things worth knowing before touching this again:

* **The ordering is the whole fix, so the test asserts the ordering.**
  `test/restore.test.ts` patches `ServerNode.prototype.start` and snapshots the
  aggregator's children at the instant it is invoked *and* when
  `lifecycle.online` fires. Asserting the endpoint set after `BridgeNode.start()`
  returns would pass just as happily with the restore in the wrong place —
  verified by mutation: moving the restore below `server.start()` fails that one
  test and nothing else; deleting it fails 9 of the 11.
* **A v1 map is migrated, never discarded.** Its entries are bare numbers, which
  is the one thing that cannot be re-derived and the thing every paired
  ecosystem is keyed on. They load, they are not restorable, the first attach
  records role/label, and the start after that one restores. Same rule one level
  down: a v2 entry with a malformed `role`/`label` keeps its number.
* **`options` is deliberately not persisted.** Nothing in the node reads it —
  window-covering polarity is applied plugin-side — so storing it would be
  satisfying a sentence rather than a caller.
* **Refusing beats restoring.** A node in a refuse-to-start state creates
  nothing, restore included. `identityUnreadable` is known before the stack
  starts and skips the restore outright; `fabricStorageLost` can only be decided
  *after* `server.start()` (it needs the fabric table), so the restored set is
  withdrawn there instead — safe by construction, because that state means no
  fabrics, so nothing was attached to see it.

**Not fixed here, on purpose:** #140 (drift reports cannot be cleared). The
restore does not make it worse — a restore is not a comparison, `driftChecked`
stays `false` until a real one runs, and re-using the map's own numbers raises
no drift.

`indigo-matter-bridge@0.6.0` **is now published** (0.5.0 and 0.6.0 are both on
the registry; `latest` is 0.6.0), so `DEFAULT_INSTALL_SPEC` resolves off npm.
The hardening below changes node source *after* that publish — see its last
paragraph.

### 2026-08-06 (later) — hardening from the two PR #142 reviews

Suites: **2252 Python**, **380 TS** (from 2243/366 on this branch — both
measured, not carried over). pylint **9.42**, unchanged. One reviewer reported
353 TS against the same claimed baseline; it is not reproducible here. The only
reproducible way to lose tests silently is running `node --test` directly
without `npm run build`, which skips `main.test.ts`'s whole `describe` on
`existsSync(dist/main.js)` and contributes **0** rather than 8 (→ 358); an
aborted file under the suite's known mDNS contention would do the same for
whatever it took with it. `npm test` itself always builds first, so under `npm
test` the number is 366/380.

**Ghost accessories — the restore's own version of the bug it fixed.** `check()`
only ever added and refreshed, so once a v2 entry carried `role`/`label` it
carried them for ever. A device the user *un-exported* therefore stayed
restorable: rebuilt as a child endpoint before every `server.start()`, removed
again by the plugin's next attach seconds later. That is exactly the
appear-then-vanish churn #141 exists to eliminate, aimed at devices the user had
already removed, and it regressed XAC7. Each ghost also spent a
`REMOVAL_PACING_MS` (100ms) slot on every attach while counting towards neither
the desired set nor the un-export debt, so an accumulation of them ate the 8s
attach floor for accessories nobody had asked for.

*Fix:* on a removal, clear `role`/`label` and **keep `number`** —
`restorable()` filters on role *and* label, so the entry goes non-restorable
while §3.3's allocation survives and one re-export refills it at the same
number, which means the same accessory in every paired ecosystem rather than a
new one. `node.ts` measures the removals by diffing the live set across one
mutation (`forgetRemoved`) rather than reading absence: an empty live set proves
nothing (a node that never attached has one, and `seed([])` knows nothing by
design), so a factory reset, a seed, and an entry this build cannot rebuild all
leave the map alone. `withdrawRestoredIfRefusing` goes through
`registry.reconcile` directly and so does not forget either — correct, since a
refusal is not an un-export.

**A permanent halt this PR made reachable, and the PR body described it
backwards.** When the classifier empties a *non-empty* allow-list — every entry
skipped because the Indigo device was deleted, stopped being exportable, lost
its declared role, or raised out of `states_for` — `endpoint_specs()` returns
`[]`, there is no debt, and the attach went out bare. Before the restore that
was harmless: nothing was ever created before the plugin attached, so the node's
live set was empty, §3.1's guard was never armed, and the empty attach was a
no-op. The restore arms the guard from the first attach, so the same routine
restart is now refused with `mass_removal_refused` — which is in
`HALTING_ATTACH_ERRORS` and halts the client permanently, every reload, behind a
message telling the user to check an allow-list that is not the problem. A
one-device allow-list whose device the user deleted reaches it unaided.

*Fix, plugin-side:* `bridge_client._replace_all` now carries `intent:
replace_all` in that case too, gated on a new `export_count_provider`
(`export_bridge._declared_export_count`, the store's own length) so a
*genuinely* empty allow-list still carries nothing and §3.1 keeps its teeth
against the stale client it was written for. A provider that raises fails
towards the guard, not past it. It is announced loudly at WARNING on both sides,
naming every skipped device — latched per cause like `_skip` is, because the
provider runs per (re)connect. The declared count also joins the attach-deadline
`max()`, since it is the only proxy for how much the node is about to remove.
Chosen over "do not attach at all": the node would otherwise serve accessories
the plugin can neither drive nor push state to, with nothing to clear it, and
§3.3's retained numbers make the removal recoverable.

**Mutation survivors closed** (all seven ⊗ verified fail-before by mutating the
source and re-running): the restored accessory's `label` (came up as "Restored
Accessory" with the suite green), `registry.restore`'s per-spec `try`/`catch`
(one corrupt entry aborted the whole restore and the bridge booted empty), the
`indigoDeviceIdFrom`/`isRole`/`isSupportedRole` skip (helper-tested,
caller-untested), the `label` half of the `restorable()` predicate (the existing
test used a bad role *and* a bad label), `check`'s refresh-even-when-the-number-
drifted branch (its own comment argued for it and nothing pinned it), and
`states: {}` ("no state value is invented").

**Also:** `bridge_agent.py`'s "not on the registry yet" corrected; §4.3 of
BRIDGE_PROTOCOL now states that of the three refusal reasons only two restore
literally nothing — `fabricStorageLost` restores then withdraws, which is
wire-observable (transient `endpointCount`, a `ConfigurationVersion` bump,
startup latency) and accepted because that state means no fabrics; `seed()`
carries an assertion that it is a full replace; `main.test.ts` pins its children
to the loopback interface and documents the residual UDP 5353 contention it
cannot fix (the port is protocol-fixed and matter.js exposes no knob).

**Publishing:** these changes are node source *after* `0.6.0` went to npm, so a
bridge installed from the registry does **not** have them. `package.json` is
deliberately left at `0.6.0` — bumping it to an unpublished version would break
`DEFAULT_INSTALL_SPEC` resolution — so releasing this needs a version bump and
publish together, in that order, plus the matching pin in `bridge_agent.py`.

---

## 2026-08-05 — E6 + E7: pairing UX, fabric management, the bridge LaunchAgent

Plugin `2026.8.3`, bridge-node `0.5.0`. Suites: **2243 Python**, **348 TS**
(from 2056/344). pylint 9.41.

> The counts above are the ones this branch actually produces. Two earlier
> numbers here were wrong and both were wrong the *reassuring* way: the TS suite
> was recorded as 345 (`npm test` reported 347 before this batch, 348 after) and
> the plugin version as `2026.8.1` while `Info.plist` said `2026.8.3`. A suite
> count is a claim about coverage and a version is what a user reads back to you
> in a bug report — both are only useful if they are measured.

### The PR #128 review batch (2026-08-05) — read this before touching E6/E7

Three reviewers converged on one family of faults, and it is worth naming
because it will recur: **claiming success from the wrong signal.** Every one of
these shipped a message that was true of some *nearby* fact and false of the
thing the user was told.

| Signal that was read | What it actually means | What was claimed |
|---|---|---|
| `ensure_installed() is not None` | "preflight passed" | "the LaunchAgent is running" |
| `is_running()` | "launchd knows this label" | "the process is up" |
| `bridge.active` | "a client object exists" | "the bridge node is running" |
| `remove_fabric` returned | "the frame came back" | "that ecosystem has been unpaired" |
| `remove_package` returned | it ran | "Removed the … package" |

The corrections, in case any of them looks like an over-reaction later:

* **`LaunchAgent.run_state()`** now returns four distinguishable states and
  `is_alive()` is the positive one. `is_running()` keeps its old name **and its
  old meaning** ("is loaded") because the bootout/leave-alone callers want
  exactly that — a loaded-but-DEAD job (issue #104's fault 2) passes it, which is
  the whole point of the split.
* **The first-run dead end.** The bridge's plist is written by exactly one place
  (`_start_bridge_agent`), which cannot get past its own preflight before the
  package exists. So on a fresh machine: export → preflight fails → no plist →
  Install/update → npm succeeds → `restart()` finds no plist → "nothing to
  restart, fix the problem reported above" (there was none) → "the restart
  FAILED — the old version may still be running" (nothing was). `menuInstallBridgeNode`
  now calls `ensure_installed()` first and only `restart()`s when it returns
  `False` (i.e. launchd left an old job alone). The old test could not catch it:
  it asserted against `Mock(restart=…True)`, which has a plist by virtue of being
  a Mock. It is now a real `BridgeProcess` on a `tmp_path` home.
* **`_stop_bridge_agent` uninstalls rather than stops.** Keeping the plist looked
  thrifty until you notice it carries `RunAtLoad: True`: after the next login
  launchd started an unpaired bridge node, with an EMPTY allow-list, advertising
  on 5540, that the plugin never started and — because the XAC1 latch is false in
  a session that never brought it up — would never stop. XG5's guarantee has to
  survive a reboot. Re-deriving the plist costs one `ensure_installed`.
* **§3.9 answers `{removed, remaining}`** on both sides (protocol, fixtures,
  docs). The already-gone case is not an edge: the unpair picker is built from
  the *cached* fabric list, so an ecosystem that unpaired us since the dialog
  opened is the designed way to reach it. The node also emits `fabrics_changed`
  (`change: "unchanged"`) on that path, because a caller asking to remove a
  fabric that is not there is demonstrably holding a stale list and this is the
  only moment the node knows.
* **The vendor-ID table was wrong on the destructive picker.** `0x1075` is not an
  issued vendor id at all (SmartThings is `0x110A`) and `0x100B` is Signify, not
  Google (`0x6006`). Apple's *second* fabric (`0x1384`, "Apple Keychain" — the
  one ADR-0005 predicted from the observed three-fabric count) was missing
  entirely. Every entry is now verified against the CSA's Distributed Compliance
  Ledger, and `test_export_agent_wiring.py` pins the overlap against the
  **vendored matter.js source** rather than mirroring our own table. On a picker
  whose Execute button removes every exported accessory from the chosen
  ecosystem, a wrong name reads as the right ecosystem.

**Scope note, so it is not attributed to the wrong milestone later:**
`bridgedInfoFor`'s identity expansion — every bridged child publishing the full
`BridgedIdentity` (vendor name/id, product name, hardware and software versions)
rather than just label/serial/uniqueId/reachable — arrived in **this PR (#128,
E6+E7)**, not in E4 with the role factories it sits beside in `endpoints.ts`.
Until this batch nothing pinned it to the root node's `BasicInformation`:
`registry.test.ts` asserted a hand-written copy of the same literal (its comment
claimed otherwise and has been corrected), so publishing Apple's vendor id on
every child left the suite green. `integration.test.ts` now compares each child
against a real `ServerNode`'s own `basicInformation`.

**Security note, and it needs a decision from Simon, not from the code.** The
pairing page serves a **live commissioning passcode** over IWS, which
authenticates only if the user has switched authentication on. While a window is
open, anyone who can reach that URL can commission the bridge — and every
exported Indigo device — onto *their* Apple Home, Alexa or Google account.
Building auth into the handler was deliberately **not** attempted (IWS owns
authentication; a second scheme underneath it is worse than none). What this
batch does instead is say so, prominently, on the page itself and in the menu's
log line. See `docs/INSTALL.md` → "Before you pair the export bridge".

**Two new bridge menu items** close the gap where the controller had recovery
exits and the bridge had none: "Reinstall the Matter export bridge (clean)…"
(safe only because `remove_package` became per-package in E7) and "Stop the
Matter export bridge…" — for the user who disables the plugin and is otherwise
left with a running node and no UI at all, since the allow-list lever needs the
plugin to be running.

These are the last two *functional* milestones. E8 is docs.

### E6 — the user surface for protocol that already existed

Everything the node needs for pairing shipped in E5: `get_pairing`,
`open_commissioning_window`, `remove_fabric`, and the §5 `fabrics_changed` /
`commissioned` / `decommissioned` / `window_closed` events. **None of it was
reachable from the plugin**, and the events were consumed by nobody — so a
bridge could be built, exported to, and never paired, and pairing activity was
invisible without polling. E6 is the surface.

Four things are shaped by what Indigo dialogs can actually do, not by
preference, and they are worth knowing before touching any of it:

1. **The pairing codes go to the EVENT LOG.** Indigo dialogs have no dynamic
   labels, so a value computed *by the callback* cannot be shown in the dialog
   that produced it. The log is this plugin's established channel for runtime
   strings, and it is also the only place a code survives being scrolled past —
   a window lasts up to 15 minutes and nobody types 11 digits right first time.
2. **The QR goes on an IWS-served page**, because a log line cannot carry an
   image. `<Action id="pairing" uiPath="hidden">` → `http_pairing`, the one
   handler in the plugin that returns HTML rather than JSON, at
   `…/message/com.simons-plugins.indigo-matter/pairing/`. The URL is logged
   beside the codes and comes from `indigo.server.getWebServerURL()`, which
   prefers the reflector then the Bonjour name — so it is reachable from the
   phone the user is holding, which is the case the page exists for.
3. **The §5.5 config readout is a read-only textfield**, seeded by
   `getPrefsConfigUiValues`, for the same no-dynamic-labels reason. It does **no
   I/O**: everything it shows comes from the last attach, the 15-second status
   poll, or the §5 fabric events. A dialog open must never block on a node that
   may be down. Both callback spellings are defined (`getPrefsConfigUiValues`
   and `get_prefs_config_ui_values`) because Indigo 2025.2's `PluginBase`
   carries snake_case aliases and which one it dispatches on is undocumented —
   getting it wrong costs a silently blank field, so both is cheap insurance.
4. **"Pair" is "open a window", not "show the code" (PRD §6).** A Matter
   passcode is not durable: the moment the first ecosystem commissions, the
   basic window closes and the original code stops working. There is no such
   thing as *the* pairing code to display.

Two states the pair action must **not** open a window in, and both would do real
harm:

- **already open** — §3.8's `assertClosed` refuses a second one, and forcing it
  would kill the code the user is currently holding;
- **never commissioned** — §3.7 says the basic window is *already* open with the
  persisted originals, so deriving a fresh enhanced code there invalidates a
  code the user may already be typing.

Both are detected by a `get_pairing` before the open, and both report the
existing code instead.

**Duration is validated, not clamped.** 180–900s is Matter's band and the node
clamps to it; being silently given 900 when you asked for 60 is a difference the
user should hear about while the dialog is still open.

**Unpair** is a dynamic fabric picker plus E5's now-verified two-gate pattern.
The picker is built from the fabric set the bridge already reported — never from
a fresh WS round trip, because a list callback runs while the dialog is opening.
Its rows name the *vendor* (`Apple (index 1)`), because a fabric's own `label`
is whatever the commissioner wrote there and for Apple that is a UUID-ish string
that tells nobody anything; the index is always shown because that is what §3.9
removes a fabric **by**. The last-fabric case E5 hardened gets its own message:
matter.js factory-resets itself when the fabric set empties, so the user has
just reset the whole bridge without using the reset menu, and reporting a
routine removal would be a lie.

### E7 — the bridge node as a managed LaunchAgent

`bridge_agent.BridgeProcess` is the second `AgentSpec`, and it is what the XOQ3
extraction was *for*: label `com.simons-plugins.indigo-matter.bridge`, package
`indigo-matter-bridge`, entry `dist/main.js`, its own logs, the **default
per-label** applied-digest marker (the controller keeps its legacy un-suffixed
one because every existing install has that file), port 5581, and an argv
builder. The `.indigo-node` install stamp stays **shared** — reviewed again here
and kept, because both agents run whatever single node this plugin resolved, so
a per-package stamp would assert a divergence that cannot exist.

**The storage path is derived, not configured, in exactly one place.**
`bridge_agent.bridge_storage_path()` is called by the agent (which passes it as
`--storage-path`), by the fabric backup (which archives it), and it must equal
`config.ts`'s `DEFAULT_STORAGE_PATH`. A disagreement here is silent: a node
serving from one directory while the plugin backs up another, discovered at
restore time.

**The lifecycle gate (XG5/XAC1) has an ordering that matters in both
directions.** The agent starts on the empty→non-empty allow-list transition,
*before* the client (a client built first spends its whole backoff dialling a
port nothing is listening on yet). It stops on non-empty→empty **after** the
un-export has landed and the socket is closed — stopping first would take the
node down with the §3.1 removal request still unsent, leaving the accessories in
every paired ecosystem and the debt pointing at a bridge the plugin has just
switched off. It is stopped even when that attach *failed*, because the socket
is closed and the list is empty either way and the debt in prefs is what carries
the un-export forward.

A latch (`_agent_started`) gates the stop: a session that never brought the
agent up never boots one out, so a plugin reload on an install that has never
exported anything touches launchd not at all, and a bridge somebody started by
hand is not taken down by us.

**A bridge-agent failure degrades export only.** The seams are contained in
`ExportBridge` (an Indigo callback must never see a launchd fault), the client
is still built when the agent will not start — refusing to build it would
replace one diagnosis with *none*, since the client's unreachable path is what
reports the outage with the node's error log attached — and every message says
in as many words that Indigo devices and inbound Matter control are unaffected.

`_bridge_agent_diagnosis` is deliberately **read-only**: launchd owns respawn
via `KeepAlive`, the loaded-but-dead revival lives in `ensure_installed`, and a
diagnostic that quietly bounced the agent on every failure streak would turn a
crash-loop into a crash-loop nobody can read the log of.

**`remove_package` is now per package** — the `TODO(E7)` in `launch_agent.py`,
closed. It used to `rmtree` the shared `node_modules` and delete
`package-lock.json` while booting out only its own label, which with two agents
meant the sibling's package vanished under a still-loaded job that then
crash-looped on module-not-found with a *matching* applied marker and nothing in
the log to say why. It is now `npm uninstall <package>` (preferred, because it
also prunes the transitive deps nothing else needs — matter.js is ~40MB of
them), falling back to deleting `node_modules/<package>` alone. The lock file is
left alone: it describes the whole install root, not one package.

The trade this makes explicit: wiping `node_modules` wholesale also happened to
fix a corrupt *shared* dependency, and this no longer does. That was never what
the menu claimed to do, and rebuilding a sibling agent's install as a side
effect of recovering this one is worse than the fault it incidentally cured.

### PRD §5.5's export switch, and the mistake it would have been easy to make

`exportEnabled` fails **open** — absent, blank, unparseable *or null* all read as
ON. That is the opposite of the controller's attestation flag, which fails
closed, and deliberately so: the harm of misreading *that* one is relaxing
device attestation; the harm of misreading *this* one is un-running a working
export for every user whose prefs predate the key.

**Switching it off does not un-export.** It drops the socket and stops the
agent; the endpoints, the pairings and the allow-list all stand, and the
accessories go unavailable rather than disappearing. Answering the switch with
the §3.1 mass removal would delete every accessory from every paired ecosystem
and take their names, rooms, scenes and automations with them — a destructive,
irreversible answer to a checkbox. Ticking it back on reconnects.
`closedPrefsConfigUi` re-runs the transition so the switch acts immediately;
changed **ports** still need a reload, and the config dialog says so.

### The QR: what was chosen and what was not

**No QR is generated.** Rendering one needs either a Python dependency (Indigo's
framework Python has no image stack and this plugin ships none) or a
hand-written JS encoder — a few hundred lines of Reed–Solomon and bit-masking
whose failure mode is a plausible-looking square that no phone can read. Neither
earns its place for a code that Apple Home, Alexa and Google all accept **typed
in**.

The page therefore shows the manual code at a size you can read across a room,
the raw `MT:` payload for copying, and a link to the CHIP project's own QR
viewer (`project-chip.github.io/connectedhomeip/qrcode.html`) for anyone who
wants to scan. Two consequences to be honest about: that link needs internet
access, and it sends the payload to a third-party page — both stated on the page
itself. The payload is URL-encoded into it, because an `MT:` string is base-38
and can legitimately contain `+`, `/` or `%`; unencoded, the tool opens on a
silently truncated payload, which is a QR that scans and means the wrong thing.

If a real QR is ever wanted, the honest options are a bundled pure-Python
encoder under `Contents/Packages/` or an SVG built server-side — both are new
dependencies, and neither was worth it here.

### Bridged-accessory identity is now complete

`bridgedInfoFor` populated only `nodeLabel`, `productName`, `productLabel`,
`serialNumber`, `uniqueId`, `reachable` and `configurationVersion`. It now also
carries **`vendorName`, `vendorId`, `hardwareVersion` + `hardwareVersionString`,
`softwareVersion` + `softwareVersionString`** — parity with matterbridge, and
more accessory detail in every ecosystem's device pane, which otherwise reads
blank or "Unknown" for facts the bridge does know.

Verified against `@matter/types` 0.17.8's
`bridged-device-basic-information.d.ts`: **every one of these is optional on the
bridged cluster** (only `reachable` is mandatory), so all six are permitted.
Worth knowing for next time: the bridged cluster's optional set is **not**
`BasicInformation`'s — it has no `productId`, no `partNumber`, no
`capabilityMinima`, and nothing outside the verified list was added.

The values come from a new `BridgedIdentity` struct built in `node.ts` from the
same constants and the same `softwareVersion` derivation the root node's
`BasicInformation` uses, so an ecosystem showing a child's firmware and the
bridge's own can never be shown two answers. `registry.ts`'s `productName`
option became `bridgeIdentity`. `registry.test.ts` pins the whole field set so
it cannot silently shrink. `vendorId` is written through `VendorId()` — matter.js
validates the brand at write time.

### A matter.js finding worth not re-deriving: ConfigurationVersion IS persisted

While enriching the bridged identity above, the seeded `configurationVersion: 1`
looked like a bug, and the data model says it is one:

- the **root** node's `BasicInformation.ConfigurationVersion` carries data-model
  quality `N` (`@matter/model` 0.17.8, `basic-information.element.js`), so
  matter.js persists it — a live bridge shows it climbing, and jarvis's is at 13;
- the **bridged** cluster's attribute carries **no quality at all**
  (`bridged-device-basic-information.element.js`:
  `Attribute({ name: "ConfigurationVersion", id: 24, conformance: "[Rev >= v6]" })`),
  and `BridgedDeviceBasicInformationServer.schema` extends only `uniqueId` to be
  persistent;
- child endpoints are constructed fresh on every attach.

Read together that says every exported accessory's ConfigurationVersion resets to
**1** on every node restart — a backwards jump on an attribute Matter defines as
monotonic (controllers use it to decide whether cached accessory metadata is
stale), and one matter.js guards against *only* on the root
(`#preventConfigurationVersionRegression`).

**It was measured, and it does not happen.** matter.js persists the value anyway,
independent of the declared quality. Its own store contains, verbatim:

```
root.parts.aggregator.parts.indigo-1.bridgedDeviceBasicInformation.configurationVersion = 2
```

and on the next start it is restored **over** the constructor seed — seeded `1`,
read back `2`. So the literal is only an *initial* value for the first creation
of an endpoint id, which is exactly what it should be, and there is nothing to
fix.

A persistence layer for it was built and then **reverted**: it added a
`configurationVersions` map to `endpoint-map.json`, seeded from it, and bumped on
role change. All of it was machinery for a fault that does not exist, and its
comments asserted a mechanism that is empirically wrong. What survives is one
test — `registry.test.ts` → *"matter.js RESTORES a bridged accessory's
ConfigurationVersion across a restart"* — which pins the measured behaviour
against a real stack and a real restart, so the data model cannot mislead the
next person into rebuilding the same thing.

**One genuine gap identified and deliberately not taken:** nothing ever calls the
*child's* `increaseConfigurationVersion`, so a bridged accessory's version never
moves after creation. The spec says a bridge SHALL increase it when it detects a
configuration change on that device (a role change being the obvious one). That
is a real, small conformance gap — it is not a regression, it costs nothing today
(controllers re-read on the bridge's own bump, which does fire per batch), and
fixing it is a scoped change of its own rather than something to smuggle into
E6/E7.

### Distribution: the bridge is an npm package, and it is NOT published yet

`DEFAULT_INSTALL_SPEC = "indigo-matter-bridge@0.6.0"` is a **registry** spec,
(`0.5.0` when this section was written; issue #141 bumped it, and **`0.6.0` is
not published yet**)
exact-pinned exactly as `matter-server@1.2.2` is, and installing it is
`npm install --prefix ~/indigo-matter <spec>` with a different string — which is
precisely the parameterisation the `AgentSpec` extraction bought. The plugin
bundle ships **no JavaScript**; `bridge-node/` stays a top-level source
directory in the repo and is published from there.

**`indigo-matter-bridge` is not on the registry yet.** Until Simon publishes it,
"Install/update the Matter export bridge" cannot resolve the pin and E7 cannot
be exercised end to end on jarvis.

#### Publishing the bridge node

`bridge-node/package.json` is publish-ready as of this branch: `"private": true`
was **removed** (flagged deliberately — it is what blocks `npm publish`),
`files` is limited to `dist`, `main` is `dist/main.js` (which is what
`bridge_agent.DEFAULT_BRIDGE_ENTRY` expects), `engines.node` is `>=22.13.0`, and
a `prepublishOnly` script runs `clean` then `build` so a stale `dist` can never
ship. `npm pack --dry-run` produces 35 files / ~116kB — the extra one is
`README.md`, which npm always includes regardless of `files` and which was
missing until this batch (the package's npm page was blank).

```sh
cd bridge-node
npm login                 # Simon's npm account — nobody else can do this step
npm test                  # 348 tests; publishing an untested build is the one unrecoverable mistake
npm publish --access public
```

**Every release bumps two versions in step**, and a mismatch is silent until an
install fails: `bridge-node/package.json`'s `version`, and
`bridge_agent.DEFAULT_INSTALL_SPEC` in the plugin. `test_bridge_agent.py` asserts
they agree, so the mismatch is a test failure rather than a field report.

**Pre-publish testing** (dev workaround, *not* the shipped default): the same
on-disk layout is reproduced by a local install —

```sh
npm install --prefix ~/indigo-matter /path/to/indigo-matter/bridge-node
```

which puts the package at `~/indigo-matter/node_modules/indigo-matter-bridge`
exactly where `LaunchAgent._server_entry()` looks for it. `npm link` works too.
Do this and everything in E7 runs unchanged; just remember the plugin's install
menu will still try the registry spec.

**So the managed LaunchAgent is not, strictly, blocked on `npm publish`.** It is
blocked on the package being *installed*, and the local-install recipe above
installs it. What the publish unblocks is the shipped route — the
"Install/update the Matter export bridge" menu item — which is what a user would
use and is therefore what "end to end" means. Do not restate the blocker as
"npm" when the distinction matters.

### Live validation on jarvis — what has actually happened

Recorded here because README, MATTER.md and INSTALL.md all point at this file
for the specifics. Dates are the observation dates, not the write-up dates.

**E0's gate: PASSED (2026-08-04).** An uncertified bridge was commissioned into
a real Apple Home and the uncertified-accessory prompt was accepted. This is the
question that could have killed the feature outright — *will any ecosystem pair
an uncertified test-VID bridge?* — and the answer is yes. Note the pairing was
driven from the **bridge node's own console**, before the plugin's pairing menu
existed; see the outstanding list below.

**E3's live E2E: PASSED (2026-08-05).** An on/off light and a dimmer, exported
through the **Manage Matter Exports…** dialog, controlled from Apple Home and
from Indigo, each direction showing up on the other side. That is XAC4 in both
directions and XAC3's Apple Home control.

**E5 deployed to jarvis (2026-08-05), and the upgrade migration observed.**
Three exported accessories kept endpoint numbers **3, 5 and 4** across a plugin
and bridge-node upgrade — their pre-upgrade values, and deliberately *not* in
creation order, which is what makes the observation worth anything. No duplicates
appeared in Apple Home. That is XAC5's upgrade leg.

**Still outstanding, and it must stay marked so:**

- **Pairing through the plugin's own menu.** E0's pairing was driven from the
  node's console. *Pair Matter Bridge…* → the IWS code page → Apple Home has not
  been walked end to end.
- **The second-admin / multi-fabric proof** — XAC3's second half. No second
  controller has been added alongside Apple Home.
- **XAC5's reboot leg.** A plugin reload and a bridge-node restart have both been
  survived; a full Mac reboot has not.
- **E6 and E7 end to end**, which need the install menu and therefore the npm
  publish (or the local-install recipe above for a dry run).

### Deferred / not done

- **XAC3 is PART-verified — do not read this list as "nothing has been
  validated".** The pairing gate and live two-way control have both been done on
  jarvis (see the section immediately above); what is unverified is pairing
  driven from the *plugin's own menu*, and the second-admin half. The jarvis
  script below is still the route for those.
- **A locally-opened commissioning window still does not update the
  `AdministratorCommissioning` cluster's `windowStatus`/`adminFabricIndex`.**
  Unchanged from E5: matter.js 0.17.8's cluster command asserts a remote
  authenticated session, so §3.8 drives `DeviceCommissioner` directly. A
  conformance gap, not something the pairing flow depends on — it was listed as
  "for E7 to close" and is **not** closed, because closing it means either
  patching matter.js or faking a session, and the flow works without it.
- **Changing the bridge ports still needs a plugin reload.** The switch acts
  immediately; the ports are read when the client and the agent are built.
- **Restoring the bridge-node storage from a backup** is now *possible* (E7
  gives the stop/start seam the E5 note said was missing) but is **not wired**:
  `fabric_backup.restore_backup` still reports and skips the `bridge-node/`
  members. Wiring it is a small, self-contained follow-up.
- **The published Field Notes site does not know export exists.**
  `docs/matter.html` is a hand-built page generated from an earlier `MATTER.md`
  and carries **zero** export content, and `docs/index.html`'s standfirst still
  offers "two long reads" with no route to the export material or to
  `INSTALL.md`. E8 therefore points README's export lede at `docs/MATTER.md`
  directly rather than at `matter.html`. **Regenerating both pages is
  outstanding**, and until it is done the GitHub Pages site under-sells the
  feature to anyone who starts there.
- **Still open from earlier:** #105, #83, #84, #62 follow-up, #43, #46, #21–#24.

### ~~Open, unexplained, and Apple-side: room changes in Apple Home~~ — SOLVED, issue #141

Observed on jarvis with E5 deployed and 3 accessories live (2026-08-05): on/off
control works, but Apple Home refuses to change an accessory's **room** ("can't
change settings"). Simon had changed those same accessories' rooms successfully before,
so this is a change in Apple-side state, not in bridge behaviour.

**This section's conclusion was wrong and is kept for the reasoning, not the
verdict.** It said no bridge-side change could address it, on the grounds that
room assignment never crosses Matter and the node log showed no traffic at the
moment of the attempt. Both facts are true; the inference from them was not.
The node log was silent because *nothing was being asked of it then* — the
damage had already been done at the previous restart, when the node came online
with an empty aggregator and Apple re-created every accessory as new (see the
2026-08-06 section at the top of this file). "No traffic during the symptom" was
read as "nothing bridge-side is involved", and the interval that mattered was
never looked at. Fixed in plugin `2026.8.4` / bridge `0.6.0`.

The `isBridged`/`uniqueIdentifiersForBridgedAccessories` hypothesis raised at the
time is still **unsupported** and was not what fixed it; do not carry it forward.
The `BridgedDeviceBasicInformation` enrichment above is likewise unrelated.

### Deploying E6 + E7 to jarvis

**E5 is deployed** (2026-08-05, with three accessories live — see *Live
validation on jarvis* above). **E6 and E7 are not.** In order, because each step
gates the next:

1. **Publish `indigo-matter-bridge@0.6.0`** (above; `0.5.0` is published,
   `0.6.0` is what `DEFAULT_INSTALL_SPEC` pins since issue #141). Without it
   step 4 fails.
   For a dry run, use the local-install workaround instead.
2. **Install the plugin bundle** — `2026.8.4`. This is an *update* to an
   existing install, so a copy of the changed files plus a plugin restart is
   enough; only a first-time install needs the double-click.
3. **Configure ▸ Matter export.** Confirm the new Export section renders, that
   "Enable Matter export" is ticked, and that the readout line appears. If the
   readout is blank, Indigo dispatched neither callback spelling — say so, do
   not guess.
4. **Plugins ▸ Matter ▸ Install/update the Matter export bridge.** Watch for
   "Matter export bridge installed"; it will refuse to start anything while
   nothing is exported, which is correct (XG5).
5. **Export one relay** in "Manage Matter Exports…". This is XAC2: the agent
   should install, the LaunchAgent should load, and the log should show
   "bridge node LaunchAgent is running" followed by "bridge node attached".
   Verify with `launchctl print gui/$(id -u)/com.simons-plugins.indigo-matter.bridge`.
6. **Plugins ▸ Matter ▸ Pair Matter Bridge…**, 900s. The log must carry the
   manual code, the `MT:` payload, an expiry, and the page URL.
7. **Open the page URL on a phone** and check it renders. Then **add the bridge
   in Apple Home** from the manual code — expect and accept the uncertified
   warning. This is **XAC3**, the one thing no test can answer.
8. **Re-open Configure…** and confirm the readout now names Apple.
9. **Unpair an Ecosystem…** against a *second* fabric if one exists; against the
   only one it factory-resets the bridge, which is correct but means re-pairing.
10. **Empty the allow-list** and confirm the accessories leave Apple Home
    (XAC7) *and* that the agent stops
    (`launchctl print …bridge` should report the label as unloaded).

**What Simon must do by hand, and nobody else can:**

- **`npm publish`** — his npm account (step 1). Everything else is blocked on it.
- **Pairing in Apple Home** (step 7) — the uncertified "Add Anyway" prompt is
  an interactive decision on his phone.
- **Deciding whether to keep the bridge paired** before step 9/10, since both
  are destructive to the pairing he has just made.
- The plugin bundle install itself, if he would rather not have it copied over a
  running install.

---

## 2026-08-05 — E5 hardening, ROUND TWO: the PR #126 whole-PR reviews

Plugin `2026.8.0`, bridge-node `0.4.0`. Suites: **2056 Python**, **344 TS**
(from 2025/316 at the round-one batch below).

Three independent expert reviews of the **whole** PR — code correctness, silent
failures, and test coverage measured by 136 mutations, of which **37 survived**.
The theme of the survivors is one shape, and it is worth naming because it will
recur: **the helper is covered, the real caller is not.** `fabric_backup` has a
thorough suite and its only call site was unpinned. `_resubscribe_tick` has four
tests and nothing called it from `_health_tick`. The queue counters had tests
that set them by hand and nothing drove them through a dispatch. In every case
deleting the wiring left the suite green and the feature dead.

The five that would have hurt a real user:

1. **The confirm gates were inert *in the tests*, live in production.** Deleting
   the confirm check from either recovery menu left the suite green — and with a
   live client the destructive action really ran on an unticked box. The gates
   themselves were correct; the tests never set `plug.export_bridge`, so the
   connection check one line below filled the same `errors` key and the
   assertion could not tell the two apart. Now asserted against the client
   (`factory_reset.assert_not_called()`), which is the only question that
   matters.
2. **`replace_all_provider` was a `bool` where a count is required.**
   `BridgeClient` declares `Callable[[], int]` and sizes the discharge attach's
   deadline from it, because that attach sends *nothing* while removing
   *everything* — so `len(specs)` is the wrong number by construction.
   `_owes_replace_all` returned `bool`, and `int(True) == 1` sailed through
   silently: every discharge got the 8s floor, an 80-accessory un-export timed
   out mid-reconcile, tore down the socket and retried forever — the exact
   regression the deadline formula was added to prevent. The wrapper is gone;
   `_pending_replace_all` (a count) *is* the provider. The two "un-export of %d
   accessory record(s)" log lines told the truth for the first time as a
   side effect.
3. **The §4.3 warnings channel had no reader.** `BridgeClient.get_status` had
   zero production callers, `health_tick` was explicitly "no I/O", and four
   docstrings plus BRIDGE_PROTOCOL §4.3 all said it was polled. Three of the
   four faults it exists for happen *after* the attach — the identity witness
   write on first commissioning, the witness clear on `factory_reset`, the map
   write from `upsert`/`remove`'s drift check — so they reached the user as
   nothing at all. `health_tick` now polls `get_status` once per ~15s tick while
   attached, fire-and-forget, and surfaces warnings once per streak.
4. **The identity refusal lasted exactly one restart.** `quarantineIdentity`
   renames `identity.json` aside; the next start saw ENOENT, read it as a first
   run, minted and **wrote** a new identity, and served under a `SerialNumber`
   no ecosystem has ever seen — with only the routine first-run log line.
   `identityProblem` now also refuses when the file is absent *and* a sibling
   `identity.json.unreadable-*` exists, so the refusal is sticky until a human
   restores, repairs or deliberately deletes it. Relatedly, `refuse()` printed
   "until the endpoint map is rebuilt" for **every** reason including
   `identityUnreadable`, where the rebuild explicitly throws — one error code,
   two opposite remedies (now a table in BRIDGE_PROTOCOL §1.1, mirrored as
   `bridge_protocol.REFUSE_IDENTITY_UNREADABLE` and branched on in
   `_on_attach_refused`).
5. **`rebuild_endpoint_map` reported the opposite of what happened.** The node
   writes the map and clears its refusal *before* answering, so by the time the
   inline re-attach runs the irreversible half is done. If that attach failed,
   the menu said "FAILED — the bridge node is unchanged and still refusing to
   export" (both clauses false), `self.recovery` was never cleared so every push
   went on being dropped, and the user was invited to repeat an operation that
   duplicates accessories in every paired ecosystem. Recovery is now cleared the
   moment the report lands; the re-attach is triaged through
   `_handle_attach_refused` instead of bypassing it; and the two outcomes are
   reported separately. The menu deadline is `rebuild_timeout_for(n)` rather
   than a flat 45s that expired mid-re-attach past ~90 endpoints.

Also fixed:

- **The un-export debt accounting.** `_last_export_count` overwrote rather than
  accumulated, and `_record_pending_replace_all(0)` **popped** the pref — so an
  add-then-remove cycle after a debt-driven reconnect erased an outstanding debt
  of 5 with nothing having discharged it. Recording now takes the max and never
  writes 0; clearing is a separate `_clear_pending_replace_all`, reachable only
  from the two places that watched an intent-carrying attach succeed. An
  unreadable pref value is logged rather than silently read as "nothing owed".
- **The resubscribe watchdog gave up with a bare `return`, at no log level.**
  The one feature whose purpose is to break a silence ended by going silent. It
  now says so once, naming the consequence. `_device_updates_seen` still never
  re-arms — documented as a deliberate limit, because re-arming would make every
  quiet house warn that export is broken.
- **`noteFabrics`' bare `catch { return }`** swallowed the failure, stranded
  `commissionedAt` (so a later deliberate last-fabric unpair refuses with
  "fabric storage is gone") and dropped every `fabrics_changed` /
  `commissioned` / `decommissioned` event. It logs now, in the voice of
  `node.ts:208`, and still never rethrows.
- **M11:** the rebuild menu gated only on `client.connected`. Run against a
  healthy node it silently discarded the retained endpoint-number allocations of
  every non-live export — the ones that make re-adding a device restore the same
  accessory. It now requires a refusal to be in force, and the dialog says so.
- Plus the M-series: ignored `persist()`/`seed()`/`discard()` return values,
  `factory_reset`'s witness verification passing on an *unreadable* identity,
  `driftChecked` going true off an empty comparison, the corrupt-map quarantine
  gaps (`persist()` overwriting without one; a fixed `.corrupt` name that a
  second quarantine overwrote), the command deadline saying out loud that it
  does not correct the ecosystem, and the ws-server's unattached timer never
  re-arming after a refusal clears.

### Deferred, with reasons

- **`COMMAND_TIMEOUT` / `COMMAND_QUEUE_WARN` constant-scaling mutants are not
  pinned** (30s → 10 hours; 3 → 100000). The only test that distinguishes them
  is a wall-clock assertion, which trades a real flake for a synthetic mutant.
  The *logic* around them — the `shield`, and both queue counters — is now
  driven through the real dispatch path and dies on mutation.
- **`get_status` is polled, not threaded through the `factory_reset` /
  `upsert` / `remove` responses.** Those are protocol shape changes (§3.2/§3.10
  return `{endpointNumber}` and `{}`) needing golden-frame and doc churn on both
  suites, and the 15s poll already reaches every fault they would carry.
- **`main.ts`'s `clearTimeout(escapeHatch)` is an equivalent mutant.** Deleting
  it keeps the suite green, and correctly so: the hatch is `unref()`'d and
  `process.exit(0)` runs synchronously on the next line, so cleared-vs-uncleared
  has no observable difference. The only way to expose it is to also delete the
  exit — which is a *different* mutation, and that one dies. `main.ts` was not
  refactored to manufacture a seam for a line with no behaviour.
- **A5's last-fabric path and the `commissionedAt` write remain hardware-gated**
  end-to-end — unchanged from round one, and still on the jarvis script. The A1
  bootstrap is no longer among them: `PosedNode` (a read-only Proxy over the two
  things `node.ts` actually reads about commissioning — `lifecycle.isCommissioned`
  and `state.commissioning.fabrics`) covers it at node level without a fabric.
- **One pre-existing test was wrong and is corrected.** `"runs after a reconcile
  that failed part-way through"` never part-applied anything: `reconcileNow`
  validates every role up front, so its `role: "notARole"` threw before a single
  endpoint was created, and it passed only because `check([])` flipped
  `driftChecked` — the exact bug M3 fixes. It now uses an over-long `NodeLabel`,
  which genuinely builds endpoint 1 and throws on endpoint 2.
- **Still open from earlier:** #105 (bridged-endpoint real-bridge validation),
  #83, #84, #62 follow-up, plus the older #43, #46, #21–#24. `remove_fabric`
  still has no UI; it needs E6's fabric readout to pick an index from.

---

## 2026-08-05 — E5 hardening: the PR #126 three-review batch

Applied on `feat/e5-persistence-hardening` on top of the E5 commit below.
Plugin `2026.7.31`, bridge-node `0.4.0`. Suites at this commit: **2025 Python**,
**316 TS** (from 1978/291 at the E5 commit).

Two independent expert reviews plus a test-coverage review; 5 Criticals, ~10
Highs, 5 Mediums and 9 coverage gaps. The five that would have hurt a real user:

1. **A1 — the deploy blocker.** `refuseReasonFor` refused a commissioned bridge
   with **no** `endpoint-map.json`, and that is the state of *every* install
   commissioned before E5, because the file did not exist yet. jarvis today has
   2 fabrics, 4 endpoints and no map: deploying E5 as written would have refused
   the attach and stopped serving all four accessories. And it bought nothing —
   **matter.js owns the numbers**, this map only witnesses them, so a missing
   witness renumbers precisely nothing. Absent-on-commissioned is now a
   **BOOTSTRAP**: seed the baseline from matter.js's own persisted allocation
   (read through `ServerNodeStore`, guarded, falling back to the first attach's
   live set) and serve, logged as a migration. Only a *present-but-unreadable*
   map still refuses.
2. **A2 — the recovery exits did not exist.** `rebuild_endpoint_map` had no
   caller anywhere in the plugin, while three user-facing strings told the user
   to confirm the rebuild "in the plugin". Two menu items now exist:
   **"Rebuild Matter Endpoint Map…"** (§3.11, one confirm + the duplication
   warning) and **"Reset Matter Export Pairings…"** (§3.10 `preserve=true`, two
   confirms). Both gate on `client.connected`, not `attached` — §1.1 holds the
   socket open un-attached and that is the only state a rebuild is needed in.
   `remove_fabric` still has no UI; it needs E6's fabric readout to pick an
   index from, and is deliberately deferred there.
3. **A3 — the refusal destroyed the thing it protects.** `main.ts` called
   `identityProblem()` and then `loadOrCreateIdentity()` unconditionally, and
   the mint writes through `rename` — so an unreadable `identity.json` (the
   `SerialNumber`/`UniqueID` every ecosystem knows us by, unregenerable) was
   obliterated one line *before* the refusal that exists to protect it. Now:
   move it aside to `identity.json.unreadable-<stamp>`, mint **in memory only**,
   write nothing. `rebuildEndpointMap` also refuses to clear an
   `identityUnreadable` refusal — different loss, different remedy.
4. **A4 — pending debt + a disjoint re-add was a permanent halt.**
   `_owes_replace_all` was ANDed with an empty allow-list. Empty the list while
   the node is down (debt = 5), export a *different* device: the attach carried
   no intent, the node's §3.1 guard saw 5 removals / 0 survivors, answered
   `mass_removal_refused` — which HALTS — and blamed an allow-list that was
   never the problem. Nothing retries a halt. The debt now answers
   independently of store emptiness, and a `mass_removal_refused` reached while
   a debt is recorded retries **once** with the intent instead of halting.
5. **A5 — `remove_fabric` on the last fabric.** matter.js self-factory-resets
   when the fabric set empties (`CommissioningServer` → `doFactoryReset()` →
   `erase()`), and we never cleared our witness — so the next boot refused with
   `fabricStorageLost`, blaming lost storage for a deliberate unpairing. Handled
   in `noteFabrics`/`noteLastFabricGone` rather than in the command, so the
   route where an *ecosystem* unpairs us is covered too.

Also worth carrying forward:

- **`StatusReport.warnings: string[]` is new in §4.3.** The node writes to
  stdout and in this milestone is started **by hand**, so stdout is a terminal
  nobody is watching — a map it could not write was invisible. The plugin
  surfaces warnings once per streak on attach and in the export dialog.
  `persist()`/`markCommissioned()`/`clearCommissioned()` now report whether the
  write landed; `rebuild_endpoint_map` **fails** rather than answering
  "serving normally again" over a map that never reached disk; `factory_reset`
  re-reads `identity.json` to verify the witness is gone before reporting
  completion.
- **`driftChecked` no longer lies over a RAM-only baseline.** A `#dirty` flag
  keeps it false while the map owes the disk something, and the next `check()`
  retries the write even when it added nothing.
- **§5 `fabrics_changed` / `commissioned` / `decommissioned` are actually
  emitted.** All three were declared, documented, carried by golden frames and
  consumed by the plugin's client — and sent by nothing.
- **The §5 command executor has a 30s deadline, submitted/completed counters
  and a queue-depth warning.** One wedged Z-Wave call used to block every
  command for every device with zero log output.
- **`preserveEndpointNumbers: true` preserves the ability to NOTICE, not the
  numbers** — `erase()` wipes matter.js's own allocation. §3.10 now says so, and
  the reset re-runs the drift check and reports what preservation bought.

### Where the coverage still is not

- **A5's last-fabric path has no automated test.** Reaching it needs a real
  commissioned fabric, which the suite cannot create without hardware — the same
  constraint that made `refuseReasonFor` a pure function. It is on the jarvis
  script.
- **`commissionedAt` is only ever written by the live fabric-changed listener**
  (`node.ts`). Every refuse test injects it into the constructed identity, so
  only a real pairing exercises the write.
- **`mapUnreadable` and the A1 bootstrap are unreachable end-to-end** without a
  real fabric: the unit tests cover `refuseReasonFor` and `EndpointMapStore.seed`
  directly, and `persistence.test.ts` covers everything a test *can* commission.

---

## 2026-08-05 — E5 (endpoint persistence), branch `feat/e5-persistence-hardening`

Plugin `2026.7.30`, bridge-node `0.4.0`. Suites at this commit: **1978 Python**,
**291 TS** (from 1947/239). `tests/fixtures/bridge_protocol/frames.json`'s
`pending` section is now **EMPTY**; the TS suite asserted it here and the Python
suite gained the matching assertion in the hardening batch above.

### The shape of the thing, because it is not what the protocol implies

**Our endpoint map does not allocate endpoint numbers. matter.js does.** It
keys its own persisted numbers on the string `Endpoint.id`, which
`endpointIdFor` makes a pure function of the Indigo device id, and that is what
actually holds identity together across a restart. `endpoint-map.json` is an
independent **witness** of what those numbers were last time, so that the one
failure that silently duplicates every accessory in every paired ecosystem —
matter.js's storage being lost, moved or reset underneath us — becomes a
sentence in the log instead of a mystery in someone's Home app. Report-only, per
§4.3, and the baseline is deliberately **not** updated when drift is found: move
it to match and the next pass calls the same fault clean.

**Identified and NOT taken: pinning the numbers.** `Endpoint.Configuration` in
matter.js 0.17.8 accepts a `number`, so the map *could* be authoritative rather
than a witness — which would make `factory_reset preserveEndpointNumbers: true`
mean what its name suggests, since `erase()` wipes matter.js's own allocation
and the preserved map currently only preserves the ability to *notice*. Not done
here because §4.3 says drift is never auto-repaired and pinning is auto-repair
by another name; it would hide the storage loss that caused it. Worth revisiting
as a deliberate protocol decision, not as an implementation detail.

**Refusing is a protocol state, not a process exit.** The Matter stack still
starts, so `get_pairing` answers and the user can see where they are; what is
refused is `attach`, which is what actually prevents the duplication — no
attach, no endpoint created. Three ways in, and the third needed a new durable
fact:

| Condition | Reason |
|---|---|
| fabrics exist, `endpoint-map.json` **present and unreadable** | the numbers being served cannot be checked against a baseline we know exists |
| `identity.commissionedAt` set, no fabrics | PRD §7 "storage missing but previously commissioned" |
| `identity.json` present but unusable | minting over it changes the `SerialNumber` every ecosystem knows us by |

> **Corrected by the #126 batch.** The first row originally read "unreadable **or
> absent**", and that was wrong in the direction that breaks deployments: every
> bridge commissioned before E5 has fabrics and no map file, so an upgrade
> refused and stopped serving working accessories. Absent-on-commissioned is now
> a bootstrap, not a refusal. The claim below that "an upgrade never refuses"
> was true only of the `commissionedAt` field, never of the map.

`commissionedAt` is a new optional field on `identity.json`, stamped the first
time a fabric is observed. It has to live there because the failure it witnesses
*is* matter.js's storage having vanished — anything stored inside that storage
cannot tell "never paired" from "paired, then the directory was lost". Absent on
pre-E5 identities, which reads correctly as "no evidence of *commissioning*" —
though on its own that was never enough to stop an upgrade refusing, because the
missing **map** did it instead (see the correction above). Cleared by
`factory_reset`, by `rebuild_endpoint_map`, and — since the #126 batch — whenever
the fabric set empties, or the reset would refuse to itself on the next start.

The refusal decision is `endpoint-map.ts`'s `refuseReasonFor` — one pure
function, no matter.js — because the case that matters most (fabrics exist +
broken map) cannot be reached in a test without commissioning real hardware.

### Two matter.js 0.17.8 findings

- **`ServerNode.erase()` leaves a ref'd timer that `close()` never clears.**
  Measured with `process.getActiveResourcesInfo()`: without an erase, close
  leaves only unref'd UDP handles and the process exits; with one, a `Timeout`
  survives and it does not. Consequence in production: a *clean* shutdown after
  a factory reset would have sat until `main.ts`'s escape hatch fired and then
  exited **1**, telling launchd a successful stop was a crash. `main.ts` now
  `process.exit(0)`s once both ordered closes have returned; the escape hatch
  still owns every path where a close does not return. Same cause, test side:
  `npm test` now passes `--test-force-exit`.
- **`OperationalCredentialsServer.removeFabric` asserts a remote actor**, exactly
  as `AdministratorCommissioning` does for §3.8, so §3.9 drives `FabricManager`
  → `fabric.leave()` directly. `leave` rather than `delete`: it flushes
  subscriptions and emits the leave event, so a controller learns it was removed
  instead of just losing the node.

### Plugin side — the #124 review carry-overs

- **Diffs are measured against the last state PUSHED, not against `orig_dev`**
  (`ExportHandler.diff_from`, `ExportBridge._pushed`). A tolerance compared
  against the previous *Indigo reading* only ever bounds one step: hue's ±1°
  meant a ramp of 1° per step reported "unchanged" every single time, so the
  accessory stayed where the ramp began while the lamp walked arbitrarily far
  away — no error, no log line, no bound. Against the last pushed value the same
  tolerance bounds the *total* error at 1°. Snapshots are seeded when a spec is
  built (an attach/upsert IS a push), merged rather than replaced on each
  `set_state` (§3.4 maps are partial), and dropped with the export.
- **`pendingReplaceAll` is persisted** (`matterExportPendingReplaceAll` in
  prefs, XAC7). Emptying the allow-list is the only moment the plugin ever says
  "remove everything"; if that attach did not land, nothing said it again —
  empty list → XG5 → no client → no attach → the accessories stayed in every
  ecosystem for good. The flag is written *before* the attempt (the ways it does
  not land include a reload and a power cut, neither of which reaches an
  `except`), reconnects on its own with an empty allow-list, and is discharged by
  the one successful attach, which then drops the client again.
- **§5 commands leave the loop** — `run_in_executor` on a **single** worker.
  One, not a pool: two commands for the same accessory running concurrently lets
  a `setLevel 20` overtake a `setLevel 80` and both "succeed".
- **`start()` is gated** while an un-export is in flight, and the deferred start
  is picked up by that coroutine's `finally` — refused, not dropped.
- **`_set_color_temp`'s `whiteLevel or 100` caught a real 0** (`0.0` is falsy).
  `None` means no white channel and skips the write; **0** means the channel
  exists and is off, and defaulting it turned "set the colour temperature" into
  "…and switch the white channel to full" — a bridge turning a lamp on nobody
  asked it to, in response to a command Matter defines as orthogonal to on/off.
- **`subscribeToChanges` gets a bounded watchdog.** The whole push path rests on
  one undocumented assumption (that it works the same from a menu callback as
  from `startup`), and if that is ever wrong the symptom is silence. Re-issued
  after ~1 min with an active export and no `deviceUpdated` *at all*, at most 3
  times — bounded because "no updates" is also exactly what a quiet house looks
  like.
- **Fabric backup now includes the bridge-node storage dir** under a reserved
  `bridge-node/` archive prefix (so an old controller-only archive still
  restores). Live-snapshot safety: `identity.json` and `endpoint-map.json` are
  written temp-plus-`rename`, so a reader sees one whole version or the other;
  matter.js's own store under that dir carries the same caveat the controller's
  already does. **Restore deliberately does not extract them** — that needs the
  bridge node stopped and there is no stop seam until E7 — so it warns with the
  member count, the prefix and the manual recipe rather than half-restoring in
  silence.

### Deferred items — the current state of the list

Still deferred, unchanged, both needing a `BRIDGE_PROTOCOL.md` change:

1. **No spelling for "I no longer know" (§3.4/§4.2).** A published state key can
   vanish and `set_state` cannot carry an absence, so the ecosystem keeps the
   last value indefinitely. The decision stands: keep last-known-good and make
   the gap visible (`diff_with_gaps`/`diff_from` return it, `_report_stopped_keys`
   says it once per device per streak). A real fix is a §4.2 "unknown" value or a
   §3.x `clear_state`. **Unchanged by E5** — `diff_from` reports the gap against
   what was pushed, which is if anything the more accurate question, but it still
   has nowhere to put the answer on the wire.

   **And since E5 the detection is process-scoped** (#126 C4). `diff_from`
   compares against `_pushed`, and `_pushed` is seeded from `states_for`, which
   *omits* a key that is already absent — so a sensor that was already flat when
   the plugin loaded has no baseline entry for the key it stopped reporting, and
   `_report_stopped_keys` therefore says nothing. The practical shape: a
   dead-battery sensor warns **once**, at the moment it goes quiet, and never
   again after a plugin reload. That is not a regression from anything (there
   was no report at all before E5) and it is arguably the right noise level, but
   it means "no stopped-key warnings since the reload" is not evidence that
   every sensor is reporting. A real fix rides with the §4.2 "unknown" value.
2. **Dropped invocations are not reported to the plugin.** A `lock`/`unlock`
   arriving while the plugin is away reaches no sink and there is no frame for
   telling the plugin what it missed on the next `attach`. `doorLock` still fails
   the Matter invocation so Home says the accessory did not respond; every other
   role still warns and returns. **Unchanged by E5.**

New, and deferred on purpose:

3. **Restoring the bridge-node storage from a backup** needs a bridge `stop()`/
   `start()` control — E7's launchd agent. Backup is done; restore reports and
   skips (above).
4. **Pinning endpoint numbers from the map** (the `Endpoint.Configuration.number`
   finding above) — a protocol-level decision about what `preserveEndpointNumbers`
   promises, not a patch.

### Still open from before

`bridgeWsPort` is still not in `PluginConfig.xml` (E6/E7 own the Export panel),
the bridge node is still started by hand, and **none of E5 has been deployed to
jarvis** — the XAC5 validation script is in the PR description.

---

## 2026-08-05 — PR #125 (E4) three-review hardening batch

Applied on `feat/e4-remaining-roles`. Nine findings across `export_bridge.py`,
`export_handlers.py` and `bridge-node/src/endpoints.ts`; each carries its own
regression test. The three worth carrying forward as *context* rather than as
diff:

- **matter.js's thermostat defaults are a product claim, not a protocol one.**
  Un-seeded, `ThermostatServer` enforces 7–30 °C heating and 16–32 °C cooling
  and answers `CONSTRAINT_ERROR` (status 135) outside them — so 5 °C frost
  protection and the literal `0` Indigo reports for `coolSetpoint` on a
  heat-only thermostat both failed on *every* push, forever (nothing in Indigo
  changed, so the next diff pushed the same rejected value). `thermostatDefaults`
  now seeds all eight `abs…`/`min…`/`max…SetpointLimit` attributes to 0–50 °C
  and `thermostatPatch` clamps to the same band. Related and nastier:
  `minSetpointDeadBand` defaults to 2 °C and matter.js does **not** refuse a
  write that breaks it — per spec it silently rewrites the *other* setpoint, as
  an offline write, so no `command` event is raised and the plugin never learns.
  Seeded to 0. A bridge schedules nothing and has no compressor to protect.
- **Two protocol additions were identified and deliberately NOT made.** Both
  are v2 candidates and both need a `docs/BRIDGE_PROTOCOL.md` change, which is
  why they are here rather than in the code:
  1. **No spelling for "I no longer know" (§3.4/§4.2).** A published state key
     can vanish — `sensorValue` going `None` on a dead battery, a lock's
     `onState` going `None` when its driver loses the device. `states_for`
     correctly omits it, `set_state` cannot carry an absence, and a reconnect
     does not heal it (`attach` sends the same partial snapshot; §3.4 leaves
     absent keys untouched). So the ecosystem keeps the last value it was told,
     indefinitely — a lock reading "Locked" long after nobody knows. The
     decision taken is **keep last-known-good and make the gap visible**:
     `ExportHandler.diff_with_gaps` returns the vanished keys and
     `export_bridge._report_stopped_keys` says it once per device per streak.
     A real fix is a §4.2 "unknown" value or a §3.x `clear_state`.
  2. **Dropped invocations are not reported to the plugin.** A `lock`/`unlock`
     arriving while the plugin is away reaches no sink, and there is no frame
     for telling the plugin on the next `attach` what it missed. For `doorLock`
     only, the node now **fails** the Matter invocation
     (`StatusResponseError`/`Status.Failure`) so Home says the accessory did not
     respond instead of showing a bolt that never moved; every other role keeps
     warn-and-return, because a lamp is recovered by the next attach and an
     ecosystem error there would be noise. Reporting them on attach is the
     protocol addition, deferred.
- **`endpoint.stateOf()` hands back a live view, not a snapshot.** Held across
  an `endpoint.set()`, "before" reads back as "after". Caught while wiring the
  `LockOperation` event (`applyStates` now spreads it); worth knowing before
  writing any other before/after comparison against a matter.js behaviour.

Also in the batch: the corrective push after a failed dispatch now carries the
export's §4.1 `options` (an inverted covering was being "corrected" to the
mirror image of its real position), `PressureSensorExport` converts Indigo's hPa
to §4.2's kPa (a factor of ten that rendered perfectly plausibly), and
`stopMotion` on a dimmer-modelled blind is surfaced through a new tri-state
`dispatch` return rather than vanishing into a `True`.

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

`plugin.py` lifecycle/glue + IWS `http_*` handlers + action bridge · `async_runtime.py` the loop · `protocol.py` **rename firewall** (MatterCommand/MatterWrite/MatterEvent; field names verified vs v0.6.2) · `ws_json_client.py` **shared transport** (run loop, backoff, message_id↔future correlation, disconnect + repeated-failure diagnostics; handshake/vocabulary are subclass hooks) · `matter_client.py` matter-server client on top of it (server_info + start_listening handshake, controller wrappers) · `commission_jobs.py` job state machine · `http_handlers.py` routing · `device_sync.py` reconcile + priming + state/command/write seams + node_added · `matter_model.py` parse get_node (flat `attributes` map → endpoints) · `fabric_backup.py` storage-dir backup/restore (issue #26; zip into sibling `backups/`, move-aside restore, prune) · `launch_agent.py` generic AgentSpec + LaunchAgent machinery (launchd/npm/node, XOQ3 extraction) · `server_process.py` the controller AgentSpec + prefs on top of it (launchd PM-B, off by default) · `matter_handlers/` one ClusterHandler per cluster + registry.

**Export side (E1, `docs/BRIDGE_PROTOCOL.md`):** `bridge_protocol.py` the wire contract (envelope, §3 commands, §1.1 error codes, §4.2 roles, normalised `BridgeCommand`/`StatusReport`/`PairingReport`/`FabricInfo`; **no** rename firewall — we own both ends) · `bridge_client.py` the client (hello+attach handshake, **fails closed** on `protocolVersion` skew via `on_version_skew` + halt, endpoint CRUD, fire-and-forget `set_state`, §5 event callbacks) · `bridge-node/` the TypeScript node · `tests/fixtures/bridge_protocol/frames.json` the ONE golden-frame file both suites read (§7; `npm test` copies it into the TS build).

**Export side (E2, allow-list + UI):** `export_store.py` the allow-list model (`ExportEntry` = device id + §4.2 role + name override + options; `RLock`'d, one schema-versioned JSON string in `pluginPrefs["matterExports"]`, unparseable blobs moved aside to `matterExports.corrupt` rather than discarded) · `export_catalog.py` the PRD §5.2 mapping (eligible roles + safe default, or `Excluded(reason)`; loop guard is `pluginId` only — XNG3/XAC6 — and type dispatch walks the IOM class-name chain because `isinstance` is unusable against the MagicMock'd indigo module) · `MenuItems.xml` → **Manage Matter Exports…**, the UI-D dialog: no `<CallbackMethod>` (so it gets a single Close button and the in-dialog buttons do the work), filter textfield + Apply-filter button + single-select device `menu` with `dynamicReload` (a multi-select `list` has NO CallbackMethod, so master-detail is impossible with one) + role menu + name/polarity fields + Add/Remove buttons + a readonly `exportStatus` textfield (Indigo labels cannot change at runtime) + a readonly summary list. `plugin.py` builds the store in `startup` and owns the callbacks.

**Export side (E3b, the outbound pipeline):** `export_handlers.py` the per-**role** handler table — the mirror of `matter_handlers/`, keyed by §4.2 role because outbound there is no cluster, only a user declaration. Three methods per role: `states_for` (the §4.2 snapshot), `diff` (changed keys only; hue carries a ±1° tolerance because Matter's 0–254 hue round-trips ±1°, saturation deliberately does not because 0–100↔0–254 is exact) and `dispatch` (§4.2 command → `indigo.device.turnOn/turnOff`, `indigo.dimmer.setBrightness`, `indigo.dimmer.setColorLevels`). **E3 roles only**; `handler_for` answers `None` for the E4 roles the dialog can already write into the allow-list · `export_bridge.py` the engine: owns the `BridgeClient`, starts it only while the allow-list is non-empty (XG5) and stops it on the deliberate §3.1 `replace_all` attach when it empties; its endpoint provider re-runs `export_catalog.classify` on **every** attach and skips-with-warning anything deleted, excluded, re-typed, or carrying an E4 role (an unknown role fails the *whole* attach with `internal` — E3a); `on_command` resolves through the store and dispatches on the loop thread · `plugin.py` `deviceUpdated`/`deviceDeleted` + the `_exports_changed` seam.

**E3b decisions worth not re-deriving.**
- **`subscribeToChanges` is conditional and one-way.** It subscribes to *every* device on the server ("a significant amount of traffic" per the IOM reference), so it is issued only once the allow-list is non-empty — at `startup`, or from the dialog the first time a user exports something (it is a plain request to the server, not a startup-only registration). There is **no unsubscribe in the canonical reference**, so it is never turned off again; `deviceUpdated`'s guard is a plain `frozenset` attribute on the Plugin (`self._exported_ids`, refreshed only by `_exports_changed`) so a non-exported device costs one hash lookup — no lock, no rebuild, no allocation.
- **`indigo.*` device commands from the loop thread are *unverified from the docs*.** Not "existing house discipline" — `device_sync.apply_states`'s precedent covers state *writes* on our own devices, which is a different claim from `indigo.device.turnOn` on somebody else's. It is left on the loop because `on_command` is a **single seam**: one method, one call site, so moving it to `run_in_executor` is a local change the day the loop is seen stalling. Bulk Indigo IPC does *not* get that latitude — `export_bridge.endpoint_specs` is one blocking `indigo.devices[id]` copy per exported device, and `bridge_client._gather_endpoints` runs it in an executor because this loop is shared with the inbound matter-server client (a slow Indigo server would otherwise stall live Matter updates behind an export reconcile).
- **Attach deadline scales:** `bridge_client.attach_timeout_for(n) = max(8.0, 2.0 + 0.15n)`. The node answers an attach only after its ~100ms-paced removals (§3.3), so ~80 endpoints spend 8s in pacing alone — the flat deadline would have timed out on exactly the databases that need export most. Zero protocol change.
- **…and `n` is the REMOVALS, not the endpoints sent.** For an ordinary reconnect the two track each other (the node's set came from our last attach), so `len(specs)` is a fine default. For `export_bridge._replace_all_then_stop` — the §3.1 un-export — they are opposites: zero sent, *everything* removed. It passes `attach_timeout_for(<allow-list size before it was emptied>)` explicitly, captured in `exports_changed` because the store is already empty by the time the un-export runs. Defaulting there gave a 60-device un-export the 8s floor: timeout mid-reconcile → a false "accessories may linger" warning → `close()` yanking the socket out from under a node that was working fine.
- **Colour is one-of, in both directions.** Matter's `colorMode` makes hue/sat and colour temperature mutually exclusive, and the node's push side picks hue/sat when a device reports both (`bridge-node/src/endpoints.ts` `colorPatch`). So `_set_color_temp` zeroes RGB alongside the white write and `_set_color` zeroes `whiteLevel` alongside the RGB write — each only when the device actually has that channel (`_number(dev, …) is not None`), because an all-zero RGB write to a CT-only driver is its own way to black out a room. A device with **no** white channel skips `setColorTemp` entirely (debug line) rather than inventing `whiteLevel=100` for a channel it does not have. **Needs the live E2E on a real RGBW driver** — this is the one change in the batch whose behaviour is a guess about drivers rather than about our own code. Two things to watch for on that run: (a) whether a driver treats an all-zero RGB write as "off" rather than "no colour"; (b) `colorPatch`'s mode arbitration — an RGBW device pushes `colorTempMireds` *and* `saturation` in the same snapshot, and the node sets `colorMode` from whichever it sees last, so a CT change still lands as `CurrentHueAndCurrentSaturation` with saturation 0. That is pre-existing (it happened with *stale* RGB before, which was worse), it is node-side, and it is deliberately **not** touched here.
- **Hue is omitted below saturation 20** (`export_handlers.SATURATION_HUE_FLOOR`). Indigo stores colour as three integer 0–100 channels, so hue is *recovered*, and near the grey axis those integers carry almost no angular information — measured worst-case round-trip error: 180° at sat 0, 30° at sat 1, 6° at sat 5, 1° at sat 20+. The ±1° `HUE_TOLERANCE_DEGREES` only covers the last of those, so every pastel change was pushing a `hue` nobody asked for; it converges (`setColorLevels` is absolute) but the Home colour wheel visibly jumps on the way. No protocol change — §3.4 state maps are partial by design, and the node's `colorPatch` already handles a saturation-only patch.
- **A role change in the dialog is remove+re-add** (§4.1 refuses a role change in place), and the accessory is new to every ecosystem afterwards — it loses its Home-app name and room. `exportStatus` says so before the user finds out.
- **`bridgeWsPort` is deliberately NOT in `PluginConfig.xml` yet.** PRD §5.5's Export section is a whole panel (enable/disable wholesale, both ports, the pairing readout) and belongs with E6/E7; a lone port field would ship an Export section that cannot start, stop or pair anything. `bridge_client` already reads the pref, so a hand-set `.indiPref` value is the escape hatch for E3's manually-run node.

**E2 hardening (PR #122) — read before building E3.** The store persists *then* commits: `_commit` writes the pref, flushes through the injected `save_prefs` (`indigo.server.savePluginPrefs`), and only then adopts the new map in memory, rolling the pref back if the flush raises — so memory and prefs can never disagree. It holds a `prefs_getter` callable, not the `pluginPrefs` object, because Indigo may rebind that on a PluginConfig save. A load failure is carried in `store.load_error` and shown in the dialog instead of "Nothing is exported yet.", and `matterExports.corrupt` is **first-rescue-wins** (a second corruption never overwrites it).

> **The store is NOT the guard — re-classify at endpoint-build time.** (Honoured by `export_bridge._spec_for` since E3b; keep it that way.) The injected `entry_validator` re-runs the loop guard over entries restored from prefs, and `ExportEntry.from_dict` enforces the options shape per role (`invert` only on `windowCovering`) — but both run at *load*, against the database as it was then. A device can change type, gain our `pluginId`, or be replaced between load and endpoint build. Every endpoint E3 builds must call `export_catalog.classify` again and refuse anything that comes back `Excluded`; treating a store hit as proof of eligibility reintroduces exactly the loop (XNG3/XAC6) the guard exists to prevent.

**Key invariants:** node-details has NO `endpoints` key (derive from flat `attributes`); `attribute_updated` data is `[node_id,"ep/cl/at",value]`; `node_removed` is a bare id; `server_info` is a bare connect frame (`sdk_version`/`fabric_id`). Setpoints/modes are attribute **writes**, not commands.

## 7. Cross-repo (uncommitted / coordination — left intentionally)
- `domio-code/docs/API.md` (v1.1 contract mirror) — untracked in that repo.
- workspace `docs/plans/PRD-indigo-matter-plugin.md` mirror — untracked.
- ADR Thread caveat + Domio PRD edits — flagged to Simon, not auto-applied (ADR is `accepted`/immutable).

---

# 2026-08-06 — end-of-session handover: export v1 built, live-validated, open items

**Where the export half stands: feature-complete and substantially proven on
jarvis.** Ten PRs merged (#118–#128 plus the CI marker), one open (#130, E8
docs). `indigo-matter-bridge@0.5.0` is **published to npm**. The plugin on jarvis
is 2026.8.3 with the bridge running under launchd. (Superseded later the same
day by issue #141 — see the top of this file: plugin `2026.8.4`, bridge `0.6.0`,
which still needs publishing.)

## Validated live on jarvis (dates are real, evidence in this session's logs)

| What | When | Evidence |
|---|---|---|
| E0 gate — uncertified bridge paired into Apple Home | 08-04 | "Add Anyway" accepted; the question that could have killed the feature |
| E3 — on/off light + dimmer, both directions | 08-05 | via the Manage Matter Exports dialog |
| E5 — upgrade keeps identity | 08-05 | 3 accessories returned at numbers 3/5/4, *out of creation order* = proof the map held; no duplicates |
| E5 — drift detector fires for real | 08-06 | genuine mismatch after a factory reset, reported and NOT auto-repaired |
| E6 — pairing from the plugin's own menu | 08-06 | window opened, code to log, paired in Apple Home |
| E6 — unpair + last-fabric self-reset | 08-06 | witness cleared, endpoint map preserved, node self-recovered in 200ms |
| E7 — the whole install chain from one click | 08-06 | npm install → plist → launchd bootstrap → node online → attach → reconcile |

## Still unproven (do not claim these)

- **XAC5's reboot leg.** Plugin reload and node restart pass; a full Mac reboot
  has not been done. → issue #139
- **Second admin.** No controller other than Apple has ever commissioned the
  bridge. Apple's 2-fabric count is main + iCloud Keychain, *not* multi-admin
  evidence. ADR-0007 requires this. → #139
- **IWS auth posture** for the pairing page. → #139

## The room mystery — SOLVED

Apple refusing room edits on exported accessories was **stale Apple-side
records**, not a bridge fault (room assignment never crosses Matter). Remedy,
verified: remove the bridge in Home → clear the leftover keychain fabric with
*Unpair an Ecosystem…* (Apple only removes one of its two) → re-pair. Rooms work
again. Full writeup and the docs task: **#139/#141**. Three of my theories were
wrong before this one — no-Matter-traffic, un-export identity reuse, and a
`configurationVersion` reset that a probe disproved (matter.js *does* persist and
restore it). Don't re-derive them.

## Open issues filed this session

| # | What |
|---|---|
| #131 | Sort exported devices to the top of the export picker |
| #132 | Rebuild dialog's "will duplicate" warning overstates what it does |
| #133 | `get_pairing` races the last-fabric self-reset → spurious error |
| #134 | Group the two Install/update menu items (11 apart, confusable) |
| #135 | Reset reconnect backoff after a successful install (29s dead wait) |
| #136 | Wire bridge-storage restore (E7 gave it the stop seam it needed) |
| #137 | Docs site has no route to the export half (matter.html/index.html) |
| #138 | Three screenshots wanted for the walkthrough |
| #139 | Live validation tracker + the solved room remedy |
| #140 | **No way to clear a drift report on a healthy node** — Rebuild is gated on the refusal state |

**#140 is the one to fix first.** It is reachable by following our own
documentation: a factory reset makes drift expected, and the user is then left
with a permanent warning and no supported way to clear it.

## Process notes worth keeping

- Every PR got a three-agent battery (code / silent-failure / test-coverage). The
  test reviewer's **mutation** method earned its keep repeatedly — it found ~7
  load-bearing lines whose deletion broke zero tests, a confirm dialog whose gate
  was inert, and a bridged-identity producer where publishing *Apple's* vendor ID
  passed all 346 tests.
- **Recurring defect shape, six occurrences:** a helper superbly unit-tested
  while its only production caller is untested. Brief future reviewers to hunt
  that first.
- Live testing found things no review did: the wrong-menu click, the 29s attach
  wait, the unusable QR payload, and #140. Deploy early next time.
