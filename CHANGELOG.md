# Changelog

Notable changes per release. Versions are `YYYY.R.P`; the authoritative
current version is `Info.plist`'s `PluginVersion`.

## 2026.32.1 — the bridge node logs at INFO, not matter.js's DEBUG default

- The bridge LaunchAgent now sets `MATTER_LOG_LEVEL=info`. Nothing had ever set
  a level, so matter.js ran at its DEBUG default and launchd's `StandardOutPath`
  grew without rotation — 1.4 GB in a fortnight on the reference install, 99.5%
  of it per-message MRP and subscription chatter. INFO keeps every inbound
  invoke, subscribe and read, plus the session and endpoint-ready lines. The
  level is global, so the node's own two `logger.debug` diagnostics (the HR-6
  no-op line and the #143 ghost-off note) are silenced as well; promoting them
  to `info` in the node is the follow-up if they are wanted back.
  `AgentSpec` gains an optional `env`
  tuple to carry it; the matter-server agent is unchanged (it already logs at
  INFO), so only the bridge agent is re-bootstrapped once on the first start
  after this upgrade.

## 2026.32.0 — the Thread mesh page is drawn radially with role glyphs (#346)

The Thread mesh page's map was a three-row layout (leader row, router row,
child row) with same-layer links bowed into arcs to stay visible. It now
draws radially, styled after Apple's Thread Doctor app (Simon's own
reference, approved): the leader at the centre, every other router-layer
node on a ring around it, and every remaining device clustered on an outer
ring beside whichever ring-1 node it links to. A four-reviewer pass on the
first draft is folded into every bullet below, not called out separately.

- **Radial layout**: ring 1 (owned routers, foreign routers, and any
  non-router one hop off the leader) is ordered by a three-part objective —
  fewest router-to-router links crossing each other, then fewest chords
  passing through the centre's own label, then the shortest chords overall
  — exhaustive search for up to 8 nodes, a greedy walk above that, so the
  map is deterministic (same mesh, same picture) but no longer just
  alphabetical. Ring 2 clusters each device beside its own parent's
  position on ring 1; a device with no placeable parent gets its own
  trailing sector rather than a phantom guess.
- **A glyph vocabulary, not boxes**: filled circles for the leader/router/
  end device, a hollow ring for a REED, a rounded square for a router this
  plugin doesn't own, and a square split blue/purple when that foreign
  router is also the leader ("probably your border router"). The larger
  glyphs (leader, router, REED, unidentified router) carry their own RLOC16
  (the same 4-hex-digit id a neighbour table uses) inside them; an end
  device's glyph is too small for that, so its RLOC16 — like every glyph's
  — lives in its tooltip instead, alongside a red or amber triangle badge
  when it has a bad/warn health flag.
- **Quiet edges**: straight lines now (no more arcs — each is shortened to
  start and end AT its own two glyphs, not behind them; keeping a chord
  clear of a THIRD node is the ring-1 ordering objective's job, above, not
  this). No visible label on any edge, including a "poor" one (a short
  spoke gave a label nowhere to go without colliding with the far node's
  own name) — the colour, the node's warning badge, the flags list, and
  every edge's own tooltip (a hover on desktop; on a phone, rely on the
  colour, the badge and the flags list instead) already carry the
  RSSI/LQI/frame-error reading.
- **Four display bands, not the health thresholds**: excellent (≥ -65 dBm),
  good (-77…-66), fair (-87…-78), poor (≤ -88) — the map's OWN thresholds,
  deliberately separate from `thread_mesh`'s `RSSI_WEAK`/`RSSI_BAD` (still
  -80/-90), which still drive the health flags a node actually gets. A -78
  dBm link is still "good" for health but reads "fair" on the map — the two
  scales answer different questions and are allowed to disagree.
- **A collapsible legend**, moved out of the SVG into a `<details>` block
  under the map (collapsed by default, and now with its own background —
  it was unreadable in dark mode, sat on a hard-coded light-amber box with
  no dark override): every glyph's meaning — including a role this plugin
  hasn't resolved yet, and a minority-partition leader, which now draws (and
  sizes) as a router instead of falling through to that same "role
  unknown" glyph — the four bands plus Unknown (signal not reported),
  RLOC16/Partition/Frame-error definitions, and the leader-failover and
  unowned-router notes Thread Doctor's own legend carries.
- **A phone-width pass**: a 560px minimum width inside a sideways-
  scrollable wrapper, and name/role/RLOC16 fonts up to 14/11/12px, so a
  phone pans a readable map instead of shrinking an unreadable one. Node
  text carries the same halo edge labels used to, so a line running behind
  it reads as interrupted rather than struck through; a ring-1 (or
  straight-up ring-2) label now draws on the OUTWARD side of the ring
  instead of always below, and the warning badge always sits on the glyph
  corner OPPOSITE the label, so neither a line nor a badge ever sits on top
  of label text. A name up to 24 characters is shown whole; longer ones are
  cut to 22 visible characters. The role sub-line reads plain English
  ("Sleepy end device"), not the raw internal role name. The viewBox is a
  TIGHT bounding box around the drawn glyphs and labels (no square padding)
  — sized from the ACTUAL drawn label lines, name AND role each at its own
  font size, so a short name with a long role line can no longer clip past
  the edge, and UAT feedback that a square viewBox centred on the leader
  padded empty bands around the map's (usually asymmetric) content is
  fixed the same way: not square, not centred, just as big as the content
  plus a margin.

## 2026.31.0 — the Thread mesh report and page now show the cache's age (#344)

The Thread page/report already labelled data `CACHED` vs `LIVE`, but gave no
age — the badge's timestamp is when the page rendered, not when the data was
true. matter-server's `get_nodes` carries per-node `date_commissioned` and
`last_interview` — the cache's baseline (observed spread on real hardware:
hours to six days) — and this release surfaces it, per node, everywhere the
mesh is shown.

- **Per-node cache age**: the menu report, the IWS page, and the page's Nodes
  table all now show an interview baseline (`interviewed 6d ago`) and, when
  this plugin has itself seen a live cluster-0x35 event for that node since
  it last started, a `last 0x35 update 2h ago` alongside it. **The baseline
  is a LOWER bound, never "accurate as of"** — a stale `PartitionId` has been
  observed to survive a later interview, so this names when the interview
  happened, not how fresh the data actually is.
- **Cached stays the page's default; old data is highlighted, not hidden**
  (Simon's UX call): a per-node interview baseline older than an hour gets an
  amber `chip-stale` cell, and the header gains an `oldest node baseline`
  note plus an amber banner pointing at the existing "Refresh (live)" link —
  the note also says how many nodes have NO baseline at all, so a handful of
  fresh readings can never read as though the whole fabric were freshly
  surveyed. Never shown on a live render, and never invented when no node's
  baseline is known at all.
- **The live-0x35-event stamp lives in `device_sync`, not the cluster
  handler** — the handler is also invoked while priming a device from
  matter-server's cached snapshot at plugin startup, and stamping there
  would mark stale cache "just now" on every restart, exactly the lie this
  feature exists to remove. Only a genuine live `attribute_updated` event
  stamps a node; the stamp is in-memory only, never persisted.
- A future-dated timestamp (clock skew) never reads as maximum freshness —
  it degrades to unknown (`—`), the same as an absent field, rather than
  "just now" or worse "just now ago".

## 2026.30.3 — Report Thread mesh… runs in the background; dialog text fits (#339)

Field report against v2026.30.0: the "Report Thread mesh…" dialog's intro
text rendered wider than the dialog, and with live-read on, the survey (up
to ~50 s) timed out inside the dialog itself.

- **Backgrounded**: the menu now submits the survey to the async runtime and
  closes immediately — "the report will follow in the Event Log within about
  a minute." Every failure that used to come back as a dialog error (a
  timeout, matter-server unreachable, an unaddressable fabric, a partial
  live-read failure) now logs to the Event Log instead, keeping its exact
  former wording; only the destination changed, because the dialog is
  already closed by the time any of them could fire.
- **Dialog text fits**: the intro, checkbox label/help and footer were
  rewritten as short, separate `label` fields (which wrap) instead of long
  text crammed beside the checkbox (which does not).
- **Discoverable**: the report now ends with a link to the visual Thread mesh
  map page. `_pairing_page_url`'s URL-resolution logic was generalised into a
  shared `HttpApiMixin._iws_page_url(action_id)` so both the pairing QR page
  and this report can build a page URL the same way.
## 2026.30.2 — A bridge update is no longer logged as a crash (#340)

Updating the bridge node takes it down for ~16 seconds, and the plugin
reported that outage as though the node had fallen over: five per-attempt
`connection lost` warnings, plus "the bridge node is not responding" with a
tail of `bridge-node.err.log` attached as evidence — a file that is appended
to and never truncated, so in the reported case its newest line was eight
days old.

The controller side already had the answer: `plugin._expect_restart()` opens a
~30s window, and inside it the crash diagnostic is replaced by one line and
**deferred** — the client's failure latch is re-armed so a server that never
comes back still surfaces its real error on a later cycle. The bridge was
never wired to it.

- **The bridge gets the same window.** `ExportBridge.expect_restart()` /
  `cancel_expected_restart()`, armed by the install path **before
  `ensure_installed()`** — rewriting the LaunchAgent is itself what drops the
  socket, 1.1s before the first warning and 2s before any `restart()` runs, so
  a window armed around the restart alone starts too late.
- **The window is a claim that a bounce is in flight, so anything that
  disproves the claim closes it**: an attach, an attach *refusal* (a node that
  answers is up — and a failed update is exactly when a refusal is the line
  worth reading), a `stop()` (the window belongs to the client it was armed
  for, and must not be inherited by the next one), and every failure branch of
  the install. That last is now structural — one `finally`, so a branch added
  later cannot forget, including on a `BaseException` the handler cannot see.
  Expiry is the backstop for none of those happening, on a **monotonic**
  deadline: on a wall clock a backwards NTP step (macOS steps rather than slews
  a large offset) would extend the window by the size of the step.
- **The shared transport quiets its own reconnect warnings.** Those five lines
  come from `ws_json_client`, which both peers use — so a *controller* restart
  was equally noisy, just without the stderr dump. The client now takes an
  `outage_expected` seam and drops per-attempt connect failures to DEBUG while
  the supervisor's window is open. It keeps no copy of the window: a supervisor
  that disarms it goes loud again on the very next attempt, and one whose seam
  *raises* turns quieting off and says so.
- **The controller's own install menu had the same ordering defect** it was the
  model for — it armed after `ensure_installed()`, so a slow plist rewrite
  could still print this bug on the peer the fix was borrowed from. Both peers'
  windows are now monotonic and armed at the same point.
- **Quieting is not swallowing.** The repeated-failure hook still fires on the
  same schedule inside the window; the `logger.exception` branch (an unexpected
  teardown, not a refused connect) still gets its traceback; and a handshake the
  peer *answered* is never quieted at all — an update that produced an
  incompatible node must not be quieter than one that produced no node.

Not covered: the fabric-restore path (`fabric_backup.py`) also restarts the
bridge deliberately, through a different control seam and over a much longer
outage than this window is sized for. Neither is **Stop the bridge node**,
which is the nearest neighbour of all: it removes the plist but leaves the
client reconnecting, so a user-ordered stop still ends in the stale-error-log
report — a window is the wrong tool there, because the node is not coming back
(#342). The `reap_orphan_servers` warning during a planned bounce is factually
correct — whether it should be quieter is a separate call.

## 2026.30.0 — Thread mesh diagnostics: states, a report, and a read-only map (#334)

Thread devices commissioned through Indigo/Domio are invisible to Apple's own
Thread tooling (Thread Doctor only lists HomeKit accessories), even though
matter-server already caches every node's ThreadNetworkDiagnostics cluster
(0x0035). This release turns that cache into three surfaces, all read-only
(ADR-0004 — nothing here writes to a device, now or ever) and all built on
one shared model (`thread_mesh.py`). Two of the three — the menu report and
the IWS page — also share one survey coroutine (`thread_survey.py`), so the
cache-vs-live policy lives in exactly one place; the four `matterNode`
states are different — they are fed directly by `device_sync`'s existing
per-attribute dispatch (one changed attribute at a time, live or replayed
from the cache at device creation) straight into `thread_mesh`, and never
call `thread_survey` at all:

- **Four new `matterNode` states**: `threadRole`, `threadNeighbourCount`,
  `threadLinkRssi` and `threadHealth` ("ok"/"warn"/"bad"), kept deliberately
  short (ADR-0008) — only what a trigger would plausibly bind to. Channel,
  network name and partition are **not** states: the reason is ADR-0008
  itself (a shipped state is permanent, so the bar is "would a trigger
  plausibly bind to this"), not staleness — all four shipped states come
  from exactly the same cache and are equally stale for a sleepy device.
  Channel/network/partition simply are not the kind of thing an automation
  binds to; they live in the report and page instead, where `threadLinkRssi`
  is also never written as a `-128` sentinel when unknown (it is left
  untouched, alongside the other three states, until the neighbour table
  itself is known).
- **"Report Thread mesh…"** — a new read-only menu diagnostic alongside the
  existing settable-attribute report and attribute explorer. Prints roles,
  neighbour/route links (RSSI/LQI/frame-error), the leader, foreign routers and
  health flags to the event log, with an option to live-read every sleepy
  (non-router) node first for accuracy — routers are never live-read; they are
  mains-powered and polled continuously already, so their cache is not the
  stale one. A live-read timeout on one node still prints everything else
  (from cache) and says so, rather than silently thinning the report.
- **A read-only Thread mesh page**, served over the Indigo Web Server at
  `/message/com.simons-plugins.indigo-matter/thread` — the same report as a
  self-contained HTML page with an inline SVG map (no JS, no external assets,
  readable on a phone over the Reflector): the leader, every other router, and
  every non-router placed under whichever router it links to, edges coloured
  by RSSI and labelled with LQI/frame-error, plus the flags list and a
  per-node table. `?live=1` runs the same live-read pass as the menu item,
  with its own shorter per-node timeout (12 s vs. the menu's 30 s — a
  synchronous IWS handler's worst case would otherwise near 50 s) and a
  cache-only fallback when the live pass times out outright, rather than
  giving up entirely. A matter-server failure renders as the page's own
  error banner, never a silently empty map.
- **Foreign routers are shown but not named** — a router referenced by an
  owned node's tables that no Indigo device claims is drawn as "Router N",
  the fabric's leader captioned "probably your border router" when it is one
  of them. Naming them (via mDNS `_meshcop._udp` discovery of a border
  router's own ext address) is a separate concern, tracked as #335.
- **A non-router with no parent is unhealthy, not silently "ok"** — a
  SED/ED/REED whose neighbour table is known but has no usable parent link
  now gets a `bad` `no_parent` health flag; this check used to live only
  inside the router branch, so a detached leaf node reported clean.
- **A router's children that can only be partly matched to Indigo devices
  are no longer individually mislabelled** — which specific RLOC becomes
  which "child:0x.." box was a NeighborTable-iteration-order coin flip once
  some (not all) of a router's children were already attributed to an owned
  device; the unmatched remainder now collapses into one order-independent
  "N unattributed child(ren)" box instead.

## 2026.29.15 — the bridge-node suite stops hiding tests it did not run

Fixes #330. `npm test` reported `# fail 0` and `# skipped 0` on runs where it
had silently not executed whole suites. Counts alternated between two values —
144 suites or 141, never anything else — and the missing block was always the
same: `planReconcile (§3.1)` with its `battery composition (issue #220)` and
`driving-device rekey (issue #246/ADR-0011)` children. Eight tests of real
regression coverage could vanish from a green run with nothing to say so.

The proximate cause was `--test-force-exit`, which terminates the runner while a
concurrently-forked file is still streaming TAP. But that flag was load-bearing:
without it the suite hung indefinitely. Two leaks were holding the loop open.

**Every interface, not just loopback.** matter.js advertises mDNS on every
interface the host has, VPN tunnels included, and a send queued against a tunnel
that is not carrying traffic never completes — leaving UDP 5353 sockets and
pending `SendWrap` requests behind (`UdpMulticastServer utun8: UDP send
timeout`). `main.test.ts` already pinned its spawned children to loopback; the
four in-process files that stand up real `ServerNode`s did not. They now share
`test/loopback.ts`, which also replaces `main.test.ts`'s private copy.

**A responder nothing owned.** `MdnsService` is a service on the *Environment* —
`Environment.default`, a process-wide singleton — not on any node, so it outlives
the last node closed and keeps four UDP sockets open. Verified directly: 50
nodes started, 50 closed, sockets still held. `closeSharedMdns()` disposes it in
an `after()` hook.

With the flag gone, a **pre-existing failure it had been hiding** appeared:
`persistence.test.ts` reported 47 tests / 0 fail with the flag and 48 tests /
1 fail without. `VolatileEventStore.add()` issues
`storage.set("lastEventNumber", …)` without awaiting it, so a write can still be
in flight after `ServerNode.close()` resolves; deleting the scratch directory in
that window fails `ENOENT` and surfaces as an `unhandledRejection` attributed to
whichever test ran last. Cleanup now settles first, and stays tolerant, because
only matter.js awaiting its own write could close the race properly.

Three consecutive runs now give **676 tests / 144 suites / 0 fail**, exit 0, with
`planReconcile` and both its children present every time. 676 is the highest
count ever observed under the old flag — the 144-suite mode was always the
complete set, and every lower number was truncation.

## 2026.29.13 — ships the SIGTERM fix: bridge node pinned to 0.17.2

Release of the work banked in 2026.29.12, which changed only `bridge-node` and
so could not reach anyone until the node was published and the pin moved.

- `bridge-node` **0.17.2** published to npm.
- `DEFAULT_INSTALL_SPEC` moves to `indigo-matter-bridge@0.17.2` — the one line a
  release bumps, in step with `bridge-node/package.json`'s `version`.

What that carries, in full detail under 2026.29.12: the bridge node's
`SIGTERM`/`SIGINT` handlers now sit at module scope rather than on the last line
of `main()`, so a stop arriving during startup is a clean exit 0 with an ordered
shutdown instead of death by signal with nothing closed. A Matter node that came
online during the stop is closed and named in the log; the `main().catch`
shutdown guard logs rather than swallowing, keeping the `EADDRINUSE` marker the
plugin greps for out of the dark.

Existing installs pick the new node up on the next install/upgrade pass;
`launch_agent.py` warns when a running job's arguments have drifted from the
plist, so a stale node is visible rather than silent.

## 2026.29.12 — the bridge node answers SIGTERM from its first line, not its last

**bridge-node 0.17.2** (not yet published; the `DEFAULT_INSTALL_SPEC` pin still
points at 0.17.1 and moves when it is).

`main.ts` installed its `SIGTERM`/`SIGINT` handlers as the **last** statement of
`main()`. Everything before them — reading and writing `identity.json`, the
whole of `bridge.start()`, `ws.listen()` — therefore ran on node's default
signal disposition: a stop arriving in that window killed the process outright,
with no `close()` of the Matter node or the protocol socket and an exit code of
`null` rather than a number.

The visible symptom was an intermittent CI failure (#328): `main.test.ts`
resolves the moment it reads `Protocol WebSocket listening`, which
`ws-server.ts` logs from *inside* `listen()`, a few ticks before the old
`process.on` was reached — so on a loaded runner the test's own SIGTERM
sometimes landed in the gap. Twice, on PRs touching no TypeScript at all.

Handlers now live at module scope, in place before `main()` runs a line, and
what has to come down lives in a small registry that `shutdown` **drains**
(rather than a pair of variables it snapshots — `main()` keeps running during a
shutdown, so the protocol WS can register after the drain has already begun,
and a snapshot left that socket unclosed).

The registration *point* is the load-bearing part:

- The **Matter node** registers *before* `start()` resolves, because `start()`
  returns long after `server.start()` has brought the node online — mDNS
  advertising, sessions accepted, storage lock held — and only then wires churn
  detection, session hygiene and the endpoint-identity assertion. Registering on
  the return walked a stop straight past a running node. Its closer sequences
  itself behind the in-flight start rather than racing it, since `#server` is
  assigned before `server.start()`; the escape hatch bounds that wait, so a
  start wedged past it is a forced exit 1, which for a wedged bridge is right.
- The **protocol WS** registers before `listen()` and needs no sequencing:
  `close()` reads its own handle, so it is either a guarded no-op or an ordinary
  close of a live server.

`Shutdown complete` now names what it closed. A bare line certified three
different outcomes — everything closed, *nothing* closed because the stop beat
startup, and every close throwing — and nothing outside the process could tell
them apart, which is the shape the workspace's degradation-path rule warns
about.

`main().catch` defers to an in-progress shutdown so a step that rejects on its
way out cannot turn a clean stop into exit 1 — but it **logs** rather than
returning silently. That window is reachable with a real fault in it (a SIGTERM
landing while `listen()` rejects `EADDRINUSE`, confirmed against `ws`), and the
`console.error` there is this program's only writer to launchd's
`StandardErrorPath` — the file `_err_log_mentions_port_conflict` greps for
exactly that marker. Swallowing it would have hidden the one startup fault the
plugin knows how to diagnose.

Two claims worth recording as **false**, since both were believed while writing
this. The identity file was never at risk: `storage.ts` writes it through a temp
file and `rename`, so a death by signal leaves either the old file or the new
one. And launchd never read this as a crash on the plugin's own stop path:
`launchctl bootout` *removes* the job, so `KeepAlive {SuccessfulExit: false}` is
not consulted and no respawn was ever caused by this. The real cost was always
the unclean shutdown itself. Relatedly, `node.ts`'s comment that matter.js's
signal handlers "being registered first they run first" was made stale by this
change and has been corrected — matter.js's `ProcessManager` does install
handlers, gated on a `runtime.signals` var that defaults to **true**, and is
silent here only because `BridgeNode.start()` sets it false.

Covered by a test that SIGTERMs the child on its version banner, ~90 ms before
readiness (measured — an earlier draft of this entry claimed "seconds", which
was wrong by about 20×). Startup then completes alongside the shutdown, so the
guard is the *order* of the two log lines rather than the absence of one, and
the test asserts the node that came up during the stop was closed and named —
it fails against both the pre-#328 code (exit `null`) and against the first cut
of the fix (bare `Shutdown complete`). A restart on the same storage directory
pins that an interrupted start leaves a startable box and no temp-file debris,
SIGINT gets its own case, and `start()`'s timeout path now kills the child it
used to leak — an orphan holding mDNS 5353 for the rest of the run, which can
only have worsened the contention its own file header warns about.

## 2026.29.11 — one unreadable device no longer empties the migrate picker, and a broken matter-server answer is a 500

**API change (API.md v1.5).** The `diagnostics` endpoint returned 503
`matter_server_unreachable` for *any* failure reading a node — including
matter-server answering with a protocol error, or a fault in the plugin. Since
API.md tells the client to prompt the user to retry on a 503, that sent Domio
away to try again against a server that was up and answering, over a fault that
would recur every time. 503 is now reserved for the two conditions that really
mean "no answer" — the socket being down and the read timing out — and
everything else surfaces as 500 `internal_error`, which should not be
auto-retried. `TimeoutError` is named explicitly alongside `ConnectionError`
because it is a sibling of it under `OSError`, not a subclass; catching only
`ConnectionError` would have turned every timeout into a 500.

The shortcomings #310's review recorded, now fixed — and writing the test for
one of them found a defect nobody had recorded.

- **One unreadable device used to empty the whole "Migrate an exported
  accessory…" target picker.** The per-row guard that exists to stop exactly
  that read the device's id one line ABOVE its own `try`, so a device being
  deleted underneath the dialog escaped containment and the picker degraded to
  a single "(error building list)" row — every legitimate target lost. Its
  sibling picker reads the id inside the try; these two loops describe
  themselves as the same rule and had drifted. Found by writing the missing
  test, not by reading the code.
- **The restore picker now says when it is broken.** A failed storage-path
  resolution rendered identically to "you have no backups" — the one answer a
  user acts on, by concluding there is nothing to restore. It shows the error
  row instead, and the listing itself is now covered too (its failure used to
  escape and take the dialog down).
- **A failed setting whose failure could not be recorded now warns.** The write
  failure was always reported; what was silent was the follow-up write that
  keeps the dialog honest. Without it the settings dialog re-seeds from state
  and shows a value the device never adopted — the divergence that write exists
  to prevent. A device deleted mid-flight stays quiet, as before.

Three test gaps closed with it: `matter_client`'s raising event handler (its
`bridge_client` twin has been tested for months), the migrate picker's hostile
row, and `_previous_accessory_number`'s guarantee that a malformed status
report cannot fail a migrate that has already committed — a pin deleted along
with the re-adopt suite in #274b, leaving the decision unwatched.

## 2026.29.10 — every broad exception handler in the plugin has now been read

No user-visible change, and the end of issue #310's triage. The last 40 broad
`except Exception` handlers — across the menu mixins, commissioning, the
WebSocket clients and the HTTP API — were each read with their comment, their
originating commit, the issues and ADRs they cite, and whether a test actually
pins the decision. Every one now states what it absorbs and why that is safe.

**Plugin-wide `broad-exception-caught`: 120 → 0**, pylint 9.48 → 9.59, across
2026.29.4 – 2026.29.10. Not one handler was suppressed unread.

Three comments were found to be **wrong** and are corrected here. The frame
catches in `ws_json_client`, `bridge_client` and `matter_client` each said
"must not kill the loop"; the loop is already protected by the run-loop catch
above them, and what they actually buy is "drop one frame and keep the
connection" rather than a teardown that would reconnect, re-attach and very
likely be handed the same bad frame again. The distinction matters the next
time someone decides whether one of them is removable.

Two handlers are recorded as known shortcomings rather than changed, because
fixing them alters behaviour and belongs with a test: `_apply_setting`'s device
flag (2026.29.8) and `getFabricBackups`, which renders a failed storage-path
lookup identically to "you have no backups".

## 2026.29.9 — a raising rollback restart is now covered by a test, and four more modules are reviewed

Mostly internal (issue #310). The six broad exception handlers in
`device_settings.py`, `settings_report.py`, `fabric_backup.py` and
`color_control.py` were read with their commits, issues and tests, and each now
states what it absorbs and why that is safe.

**One real gap closed.** `fabric_backup`'s rollback path — the worst case a
restore can reach, where the original fabric is back on disk but matter-server
is down — has a handler catching a raising restart so that the CRITICAL
manual-recovery message, and the bridge restart with it, still run. No test
could reach it: the test double could make that restart *return False*, but
nothing could make it *raise*, and the two arrive by different routes. The
double gained the missing mode and the path now has a test, verified to fail
when the handler stops catching.

Nothing was narrowed. The two write paths in `device_settings.py` cannot report
a swallowed failure as success — success is only ever declared by a read-back —
and `color_control`'s echo guard fails to an absent value, never a wrong one.

## 2026.29.8 — internal: every broad exception handler in plugin.py is now a reviewed one

No user-visible change. All 34 `except Exception` handlers in `plugin.py` were
read — with their comments, their originating commits, the issues and ADRs they
cite, and whether any test actually pins the decision — and each now carries a
`# pylint: disable=broad-except` and a stated reason (issue #310, step 3).

With `device_sync.py` (2026.29.6) and the energy guard (2026.29.7) now merged,
the two big files are finished and the plugin-wide `broad-exception-caught`
count falls **120 → 46**, all of it now in twelve smaller modules.

Nothing was narrowed here. The audit found one genuine candidate
(`_apply_setting`'s device flag) and it is recorded at the site rather than
changed, because narrowing it is a behaviour change and belongs with a test.
## 2026.29.7 — a node's energy reading can no longer be overwritten because Indigo was briefly unreadable

`_prime_absent_node_energy_state` flags a node device's `curEnergyLevel` /
`accumEnergyTotal` as `<no meter>` so a node without an endpoint-0 meter does
not display a bare `0` that reads as a real "0 W". It is guarded: before
writing, it reads what the device currently shows and does nothing if a real
reading is already there. `deviceStartComm` re-fires on **every** prop change,
so that guard runs constantly.

The guard's own failure was swallowed, and priming went ahead anyway. A read
that failed for a transient reason — the very moment Indigo is least
trustworthy — therefore let the placeholder overwrite a real reading.
`accumEnergyTotal` moves slowly enough that SQL Logger would record a large
negative delta immediately followed by an equally large positive one.

**The two failures are now told apart**, which is the whole fix:

- **The device is absent** (`KeyError`) — it cannot be holding a reading worth
  protecting, so the prime still happens. Leaving the state at Indigo's bare
  `0` would be the plausible-looking-value mistake this method exists to
  avoid. Unchanged behaviour, and the existing test that pins it still passes
  untouched.
- **The lookup itself failed** — the device most likely still exists, so the
  prime is skipped and the reason is logged. The display shows a bare `0`
  until `device_sync`'s own priming pass runs on the next connect; nothing is
  destroyed.
## 2026.29.6 — internal: every broad exception handler in device_sync.py is now a reviewed one

No user-visible change. All 40 `except Exception` handlers in `device_sync.py`
were read and each now carries a `# pylint: disable=broad-except` **and** a
stated reason — what it absorbs, and why absorbing it is safe. The plugin-wide
`broad-exception-caught` count falls 120 → 80; the remainder are other modules,
`plugin.py` holding 34 of them (issue #310, step 3).

The suppression is the point: it is only added where someone has read the
handler, so the count that remains is an honest backlog rather than noise.

## 2026.29.3 – 2026.29.5 — a failed device lookup is no longer mistaken for a deleted device

`indigo.devices[dev_id]` raises `KeyError` for "no such device" and anything
else — a busy server, a dropped connection — for "the lookup itself failed".
`device_sync.py` collapsed both into one handler at eight places, so a sick
Indigo server was indistinguishable from a device the user had deleted, and
the work behind the lookup was skipped with a debug line nobody reads.

Seven of the eight now say so, once per device per kind of failure (the
true-up paths run on every reconcile, so an undeduped warning would nag
forever):

- **Marking a device unreachable** — the one that looks exactly like health.
  If the error state cannot be written, the device carries on showing as
  normal while its Matter node is not responding.
- **Clearing an error state** — the mirror: a recovered device keeps a stale
  "unreachable" badge.
- **Writing a node's `reachable` state** — the most consequential, because
  `reachable` is a state triggers and schedules are written against. A failed
  write leaves those automations reading a value that has stopped tracking the
  node, and until now nothing anywhere said so.
- **Applying a commissioned name and folder**, and **both name true-ups** —
  each returns "it didn't land", which was already the safe answer, but a
  caller should not be told that in silence when the cause was a failed lookup
  rather than a failed rename.
- **Grouping a newly created device** — it is left ungrouped and nothing else
  reports it.

The eighth is deliberately unchanged: a refused **ungroup during a
decommission** stays at debug, because an open device dialog refuses it
routinely, the delete that follows is still attempted, and when *that* fails
it already warns. A test pins that level, and it caught the first version of
this change trying to escalate it.

A device that is genuinely gone still says nothing — that is not a failure,
and reconcile heals the index on the next pass.

Also in these versions, both internal: a reproduction test for issue #203 (a
bridge-node restart drops a running `OnWithTimedOff` countdown, leaving the
device on — confirmed, not yet fixed), and the deletion of 116 `# noqa: BLE001`
comments that suppressed nothing because ruff is not a dependency of this
repo (issue #310, step 1).

## 2026.29.2 — the bridge node for permanent removal

- Pins the bundled bridge node to **0.17.1**, which is where 2026.29.0's and
  2026.29.1's wire changes actually live. 0.17.0 had already been published
  when those landed, so the repo was holding the new plugin against the old
  published node — a combination that fails quietly rather than loudly:
  `protocolVersion` is still 2 on both sides, the handshake passes, nothing
  refuses, and removals simply go on orphaning. The deletion behaviour below
  would have been inert on any install that had not rebuilt the node by hand.

## 2026.29.1 — "Re-adopt a Matter accessory…" is gone

With a confirmed deletion now destroying the accessory (2026.29.0), nothing
can leave an orphan behind, so the whole concept comes out — 14 methods, the
`list_orphans` wire command, the `orphanedAt` field and the menu item itself.

- **The plugin menu loses "Re-adopt a Matter accessory…".** There is no
  longer anything for it to list. If you want to keep an accessory's identity
  and number while changing which Indigo device drives it, that is what
  **"Migrate an exported accessory…"** does, and it is unaffected.
- **Migrate is verified end to end against the new mechanism.** Its two-command
  sequence now destroys and re-adds rather than superseding in place; the
  accessory number comes from matter.js's own persisted allocation, so both the
  number and the identity still move correctly.
- **A role change's old accessory was never re-adoptable** and still is not —
  that number is retired on purpose (issue #240).
- `mass_removal_refused` is untouched, and matters more now than it did: it is
  the single guard between a plugin bug that sends a short list and a wiped
  fabric.

ADR-0016 supersedes ADR-0015's mechanism; ADR-0015's ruling stands. Closes #274.

## 2026.29.0 — a deleted Indigo device destroys its Matter accessory

**Behaviour change.** Deleting an Indigo device now removes the Matter
accessory it exported, in every paired ecosystem. Previously the accessory was
kept as an "orphan" so it could be handed to another device later.

The reason for the change is what an orphan actually costs. Issue #273 measured
it on the reference rig: **nine live published endpoints** whose Indigo devices
no longer existed — duplicate names, dead accessories and inflated endpoint
counts in Apple Home and Alexa. An orphan is not free; it is an accessory every
controller still sees.

- **If you want to keep an accessory, migrate it before you delete the device**
  — "Migrate an exported accessory…" moves the identity and the number across.
  Delete first and you re-pair in Apple/Alexa, which takes a few minutes.
- **A deletion the plugin was not running for is now caught too.** Indigo told
  nobody, so on the next attach an endpoint whose device is gone is destroyed
  rather than retained.
- **Existence is the whole rule, and it is deliberately narrow.** A device is
  gone when it is not in `indigo.devices` — nothing else. A device whose owning
  plugin is disabled, restarting or still loading STILL EXISTS, so none of those
  is ever read as a deletion. There is no grace period because none is needed.

## 2026.28.9 — a failed save now says so, and a state-removal notice stops repeating

- **A survey-log save that fails is now logged.** `_save_survey_log` had no
  error handling at all, so a raising `savePluginPrefs()` was swallowed further
  down and nobody said anything — the comment describing "the caller logs there
  if it wants to" pointed at a contract nothing honoured. It now warns, naming
  the failure and its consequence (issue #308).
- **The state-removal INFO logs on change, not on every rebuild.** Indigo
  rebuilds the state list on each plugin start, and the notice reprinted its
  unchanged answer every time — around three lines per start, measured across a
  week of jarvis logs. It now prints the first time a device loses a set of
  states, and again if a device later loses a *different* one (issue #312).

## 2026.28.7 – 2026.28.8 — internal: the repo gets a test gate

No user-visible change.

- **CI now runs pytest, pylint and the bridge-node suite** on every PR and on
  every push to main. Until now it ran only version-check and create-release,
  so the refactor below was verified by nothing but someone running pytest on a
  laptop. The bridge-node run matters because `frames.json` is the one golden
  fixture both suites read — a frame change has to satisfy both.
- **Python 3.11 is back in the matrix.** Four bridge-client tests read
  `client.status` immediately after `wait_connected()` and passed on 3.13 only
  because its event loop happened to have run the attach by then; they now wait
  for the attach itself (issue #304). Pre-existing, and invisible for exactly as
  long as no CI ran the suite.
- A drifted duplicate of the inbound Indigo test fakes was unified into
  `tests/indigo_fakes.py` (issue #306).

## 2026.28.2 – 2026.28.6 — internal: five refactor passes, no behaviour change

No user-visible change; recorded so the version numbers are not a blank.

- **Six re-derived decisions collapsed into single helpers** — the
  `SupportsPowerMeter`/`SupportsEnergyMeter` derivation alone had been written
  out at six places, the same class of defect already fixed once for batteries.
- **`server_menu_mixin` split into three mixins** (2325 lines, six unrelated
  bands), **`BridgeHealthReporter` extracted from `ExportBridge`** (2888 → 2192
  lines) and **`NodeResolver` extracted from `LaunchAgent`** — all pure moves,
  no method renamed, added or dropped.
- **`exportAddOrUpdate` 165 → 104 lines**, with accessory-identity resolution
  and the colour-temperature carry-forward given their own names.
- The `LaunchAgent` split stopped well short of what the plan called for:
  measured, the code has 48 cross-band calls, most of them mutual, in exactly
  the paths hardened against #182 and #187. Composing those would have produced
  a worse file than the one we started with.

## 2026.28.1 — learned colour-temperature bounds are now visible in the UI

Issue #293 shipped the learner and the calibration sweep, but nothing in the
plugin's UI ever displayed what they found: the export dialog's two Kelvin
fields read only the user's SEED, so a lamp whose real range had been
measured showed two empty boxes — indistinguishable from a lamp that had
never been calibrated. Six exports on the live server were in exactly that
state.

- **The two Kelvin fields now show the EFFECTIVE bounds** (learned, else
  seed, else generic per side), with a new read-only "Range shown:" line
  saying, per side, whether the number was measured from the device or typed
  by the user.
- **An unedited field is no longer saved as a declaration.** The dialog
  records what it put on screen, and writes `ctMinMireds`/`ctMaxMireds` only
  for a value that actually changed — so pressing "Add / update export" for
  an unrelated reason (a rename) can no longer copy the learner's evidence
  into a seed that would outlive it. Editing a value still declares it, and
  clearing both still returns the device to the generic range.
- **The "Currently exported" list carries the range** (e.g.
  `Cats Lamp → Colour-temperature light · 2203-5000K measured`), so whether
  a calibration took can be read for every lamp at once. Silent for the
  generic range, which every un-calibrated export would otherwise repeat
  identically.
- **A sweep that finds no clamp now records the full range instead of
  discarding it — but only where nothing else already claims that side.**
  A lamp measured as reaching the whole 153-500 mired range used to store
  nothing at all, leaving it indistinguishable from one nobody ever swept.
  It can now store `ctLearnedMinMireds: 153`/`ctLearnedMaxMireds: 500`,
  letting the plugin say the range was measured — but a no-clamp reach is
  weak evidence (the device merely reached what was asked, which a driver
  can confirm optimistically), so it is recorded only on a side nothing
  else already claims: a user's declared seed, or an earlier measurement,
  always wins. It can therefore never widen a range the user set, and never
  overwrite a clamp a previous sweep actually measured. The learner's
  "nothing new to say" guard moved with it, from "equals the effective
  bounds" to "equals the stored learned key", which is what made a
  measurement landing on the generic edge unrecordable.
- A displayed bound is clamped to what the fields accept: 153 mireds is
  6536K, one Kelvin above the 6535K ceiling, and editing the other field
  would have bounced the save over a number the plugin itself supplied.

## 2026.28.0 — app-level session hygiene against the matter.js report-routing defect (issue #283 "Finding 2")

- **New: the bridge node now closes stale CASE sessions on its own**, ahead
  of any recurrence of the Alexa subscription-staleness fault banked in issue
  #283. Built entirely through matter.js 0.17.8's public session-layer API —
  no dependency patching (`bridge-node/src/session-hygiene.ts`):
  - **Superseded-session sweep** (the core deliverable) — when a peer opens
    a new CASE session, its older ones are closed immediately, removing the
    pile-up precondition for the still-open matter.js routing defect that
    Finding 1 documents (that defect itself remains an upstream matter.js
    fix, not addressed here).
  - **Dead-session force-close** — a subscription-free CASE session quiet
    for 60s is closed.
  - **Age-based rotation** — a subscription-free CASE session older than 4h
    is closed. A session holding a live subscription is never rotated by
    age: probing confirmed a `ServerSubscription` cannot be migrated to a
    replacement session, so doing so would strand it rather than rotate it
    cleanly.
  - A fourth mechanism (a "wedge watchdog" for a controller that keeps
    MRP-acking pushed reports while it has stopped issuing new requests) was
    assessed and **not built** — the one matter.js internal that could
    distinguish "acked" from "consumed" is private with no public path to it.
  - Every closure is logged once and counted; a new `sessionHygiene` field on
    the §4.3 `get_status`/`attach` `StatusReport` reports live CASE-session
    counts per peer (issue #283's own "diagnostic to run first" recipe) and
    the cumulative close counts. Additive, no `protocolVersion` bump — an
    older plugin ignores the field. See `docs/BRIDGE_PROTOCOL.md` §0(k)-(o)
    for the matter.js internals this was probed against, and §4.3 for the
    field.
  - Plugin-side: `bridge_protocol.py` parses the new field (tolerant of
    absence from an older node); no UI/log surfacing yet.

## 2026.27.1 — a fresh re-read gates every learned colour-temperature adoption

- **Fixed: a stale caller snapshot could let a colour-temperature bounds
  adoption collapse the learned range instead of being refused** (issue
  #293 review, live-demonstrated 2026-08-24 15:39 on device 1894385558, a
  MiBoxer strip). A calibration sweep's cool-extreme adoption wrote a
  learned bound to the store; the SAME sweep's warm-extreme call still held
  its own pre-loop `entry` snapshot with empty options, so its collapse
  guard checked against the unlearned generic domain instead of what was
  actually stored — a warm candidate equal to the just-adopted cool bound
  sailed past the guard and was persisted as an invalid (215, 215) pair.
  Production self-healed a few seconds later when a settled reading
  re-widened it, but the invalid pair was genuinely stored in that window.
  `CTBoundsLearner._adopt` now re-reads the store FIRST and derives the
  guard's current bounds from that fresh read, never from a caller's own
  snapshot.
- **`ExportStore.upsert`/`replace_all` now validate the options they
  persist**, the same checks `ExportEntry.from_dict` (the load path) has
  always enforced. Previously only the LOAD path caught an invalid stored
  entry, so a bad write from anywhere else was silently persisted and only
  surfaced as a rejected/corrupt entry at the next plugin restart. A write
  that fails validation now raises immediately instead — for the learner,
  this is exactly the "could not be saved" failure its own adoption code
  already logs and recovers from.
- **Fixed: re-adopting an orphaned accessory onto a device could hard-fail
  the save when its previous export's options did not belong to the new
  role** (issue #293 review, exposed by the write-time validation above). A
  CT-light's learned colour-temperature bounds carried onto a `windowCovering`
  orphan (or a covering's `invert` carried the other way) is a legitimate
  cross-role re-adopt (`export_catalog`'s dimmer rule makes one device
  eligible for both), but `_readopt_commit` was carrying `previous.options`
  wholesale onto the orphan's role — handing `upsert`'s new validation a pair
  it had to reject. `options_lawful_for_role()` now filters the carry-forward
  using the same role tables `_validate_options` enforces: a cross-role
  re-adopt drops what the new role cannot carry (and now names the dropped
  key(s) in its confirmation log, so the loss is not silent) while a
  same-role re-adopt keeps everything unchanged.
- **Fixed: a shortfall streak could complete on a delayed duplicate echo
  answering an EARLIER dispatch, not the one it was recorded against** (PR
  #295 review, the race variant of the 2026-08-24 16:40 incident above).
  The distinct-dispatch rule compares the live commanded reference against
  the streak's own — but a duplicate of dispatch N delivered late, after a
  same-valued dispatch N+1 was already recorded (the #281 storm re-asserts
  every ~3-9s), inherits N+1's timestamp and reads as "a different dispatch"
  even though it never answered one. Timing alone cannot tell the two
  apart, so completion now also requires the streak to have been open at
  least `MIN_CONFIRMATION_GAP_SECONDS` (2.0s) — comfortably separating a
  duplicate callback's millisecond-scale echo from a genuine second
  confirmation riding the storm's re-assert cadence. An observation that
  clears the distinct-dispatch check but not this gap is inert, exactly
  like a same-dispatch duplicate.

## 2026.27.0 — physical colour-temperature bounds, learned from what the device actually does

- **Exported colour-temperature and extended-colour lights can now publish
  their REAL warm/cool limits to paired ecosystems, instead of the generic
  2000-6500K range** (issue #293). A device that permanently clamps a
  commanded colour temperature (issue #281's Hue/Innr warm-limit case) used
  to rely entirely on the 2026.26.2 commanded-push-and-tolerance workaround;
  now the fabric itself is told the device's honest range, so Apple's own
  adaptive-lighting curve stops asking for a colour the bulb was never going
  to reach in the first place.
- **The bounds are learned, not merely declared.** A user-set or plugin-
  reported range is still only a claim — nothing guarantees it matches the
  hardware (a MiBoxer strip's z2m props claimed 2200K while the silicon
  clamps at 2500K). The plugin instead watches a commanded colour-
  temperature write against the device's own confirmed echo: two consistent
  shortfalls adopt the echoed value as that side's learned bound, and any
  reading outside the current bounds immediately proves reach and widens
  them again — self-healing a wrong seed with no manual configuration. A
  stale commanded reference (anything more than 15 seconds old) is never
  read as evidence, so an unrelated Indigo-side change hours later cannot be
  mistaken for a hardware clamp.
- **"Manage Matter Exports…" gained two optional fields** — "Warmest white
  (K):" and "Coolest white (K):" — for a device's own colour-temperature
  role. Left empty, the generic range is used until the learner has
  evidence of its own; filled in, they seed the learner's starting point
  rather than being taken as ground truth.
- The ADR-0013 commanded-push-and-tolerance mechanism (2026.26.2) is
  unchanged: it is still what keeps the fabric attribute converged while the
  learner gathers evidence, and it never clamps to the learned/seeded range
  — only to the generic domain, as before.
- **Requires the bridge node update to `indigo-matter-bridge@0.16.0`**,
  which declares the effective bounds on the ColorControl cluster
  (`colorTempPhysicalMinMireds`/`MaxMireds`) when they differ from the
  generic 153-500 mired domain.
- **New plugin action: "Calibrate Colour-Temperature Bounds"** (issue #293's
  calibration extension). Passive learning only ever adopts a shortfall
  against a command the FABRIC sent, so a lamp nobody's paired ecosystem has
  driven yet has no reference to learn from, however many times its colour
  temperature changes in Indigo. This action closes that gap on request: it
  sweeps chosen (or all) exported colour-temperature lights through both
  extremes via the same dispatch path a real fabric command takes, reads the
  echo, and persists whatever it measures through the same learner used for
  passive evidence — no new mechanism, no automatic probing (still rejected
  as automatic behaviour per ADR-0014 Option C), and no lamp that stays off
  the whole time ever visibly reacts.

## 2026.26.2 — adaptive lighting no longer fights warm-limited lamps, and a colour-temperature change no longer dims them

- **Apple adaptive lighting no longer loops forever against a lamp that has
  reached its warm limit** (issue #281). A device that permanently clamps a
  commanded colour temperature (e.g. a bulb whose warmest setting is 2500K,
  clamping Apple's requested 2347K) used to echo the clamped value back
  every ~3 seconds without end, because the ecosystem's tile never matched
  what the accessory reported. After a successful colour-temperature
  command, the plugin now pushes the value it actually asked for as the
  accessory's own state, and tolerates a small (≤30 mireds) gap between that
  and the device's own clamped echo — closing the loop instead of
  re-opening it on every tick.
- **A colour-temperature change on a lit, exported lamp no longer dims or
  switches it off.** Every `setColorTemp` write used to carry the device's
  stored `whiteLevel`, which on channel-publishing drivers (z2m) does not
  track brightness — observed sitting at 0 (and elsewhere stale) while the
  lamp's real brightness was 29/49/69. That published a literal
  `{"brightness": 0}` on the wire with every write, switching an ON lamp
  OFF on each adaptive-lighting tick. The write now carries the lamp's
  live brightness instead, for any lamp that is actually on.
- **No bridge node update required for this release** — both fixes are
  plugin-side.

## 2026.26.1 — a virtual on/off device exported as a lock can now actually be locked

- **Fixed: a plain Indigo relay (e.g. a Virtual On/Off Device) exported under
  the `doorLock` role showed up correctly in Apple Home/Alexa but ignored
  every lock/unlock tap — Home briefly showed the requested state, then
  reverted** (issue #289). The export already read that relay's on/off state
  as the lock's bolt position; commands now drive the same relay the same
  way (`lock` turns it on, `unlock` turns it off), so a device like this only
  needs its own "on = locked" wiring, not a lock sub-type declaration from
  an owning plugin. A device that genuinely IS a lock sub-type (checked
  against its owning plugin's own declaration, not this plugin's) is
  unaffected — it still uses Indigo's real lock/unlock commands.

## 2026.26.0 — which paired ecosystem is churning, not just that one is

- **"Matter Bridge Health" now shows five per-fabric slots** (issue #288):
  `fabric1Name`/`fabric1Health` through `fabric5Name`/`fabric5Health`. With
  more than one ecosystem paired, `subscriptionHealth` alone could say
  "churning" but not WHICH — these slots name each connected fabric and each
  one's own health (`healthy`, `churning`, or `unknown`), so a trigger can
  target the specific ecosystem having trouble. A slot's name is the
  ecosystem's own vendor ("Apple Home", "Amazon Alexa", ...), not its Matter
  label — Apple, Alexa and Google never set one on a real fabric, so a
  label-only name would have read "fabric 1 / fabric 8 / fabric 9" for
  everyone; an unrecognised vendor falls back to its hex id, and a fabric
  that DOES carry a label shows it as a suffix. Slots are positional, not
  tied to a fabric's real index — they fill in order and repack when a
  fabric leaves, so a slot's occupant can change over time; an unused slot
  is empty. A house with more than five paired ecosystems (unusual) keeps
  the first five by fabric index and logs which were left out, once per
  streak, rather than silently dropping them.
- **No bridge node update required for this release** — unlike 2026.25.0,
  everything here is plugin-side: it reads the fabric list and per-fabric
  churn attribution the node was already sending.

## 2026.25.0 — a device for the bridge's own health, and Alexa subscription-churn detection

- **A new "Matter Bridge Health" device** now appears once export is in use
  (issue #286). Its `subscriptionHealth` state — `healthy`, `churning`, or
  `unknown` — is triggerable in an ordinary Indigo trigger, so "Alexa is
  churning its subscriptions against the bridge again" (issue #283) no
  longer requires reading the event log. `unknown` is the honest answer
  whenever nothing has actually been observed: before the first attach, while
  the bridge node is halted, recovering, or detached, or after a run of
  failed status polls — it is never read as "healthy". `churnDetail` names
  the affected controller peer(s) when churning.
- **Requires the bridge node updated to `0.15.0`** for `subscriptionHealth`
  to leave `unknown` — the churn detector lives there. Update the plugin,
  then run *Plugins ▸ Matter ▸ Install/update the Matter bridge* (same
  step as 2026.24.5). An un-updated 0.14.x node is not an error: the plugin
  reads its `StatusReport`'s absent field as "never checked" and the device
  simply stays `unknown`.
- The detection itself (matter.js session/subscription bookkeeping) is the
  bridge node's own change, described in its own release notes.

## 2026.24.6 — manual colour/CT changes no longer flash blue or switch off

- **Manual colour and colour-temperature changes on exported RGBW bulbs no
  longer flash blue, flick off, or fail to stick** (issue #282, broker
  capture attached). Setting a colour temperature used to zero the RGB
  channels alongside it, and setting a colour used to zero the white
  channel — both were real `setColorLevels` hardware writes, so a
  channel-publishing driver (z2m Bridge) put the zero on the wire and the
  bulb actually executed it: a deep-blue flash on a CT write, an off flick
  on a colour write. Coherence for the bridge node's colour-mode belief
  (which of colour-temperature vs. hue/saturation it should trust) now
  lives on the reporting side instead — the export publishes exactly one
  colour channel per snapshot, chosen by the device's own colour mode — so
  the write side no longer has to touch a channel nobody asked it to
  change. Interacts with issue #281's still-open re-assert loop but does
  not close it. Known limitation: a non-z2m RGBW driver that never reports
  a colour mode and leaves RGB levels populated after a colour-temperature
  write can keep the exported tile showing hue/saturation instead of CT —
  the wire command is still correct, only the reported tile is affected;
  see the export handler's mode heuristic for when to revisit.

## 2026.24.5 — the bridge node for the OccupancyChanged fix

The Node half of 2026.24.3's occupancy event fix (issue #276) — pins
`indigo-matter-bridge@0.14.0`. Update the plugin, then run *Plugins ▸
Matter ▸ Install/update the Matter bridge*.

Live-verified on the reference rig: with this node installed, motion from
an exported occupancy sensor renders in the Alexa app, which it never did
before.

## 2026.24.4 — a warning before you export a leak sensor to Alexa

- **The "Manage Matter Exports…" dialog now warns when you pick the Water
  Leak, Freeze or Rain Sensor role** (issue #278). Measured on the reference
  rig: Alexa cannot model the Matter Water Leak Detector device type, and
  exporting even one causes Alexa to either stop subscribing to the *entire*
  bridge — every exported device goes stale in Alexa, not just this one — or
  to silently drop its own pairing. Apple Home is unaffected. The warning is
  shown before the export goes live, so this is not a behaviour change for
  anyone already exporting one of these roles — just the label a future
  export now carries. `docs/MATTER.md`'s Alexa conformance section gains a
  matching note.

## 2026.24.3 — exported motion sensors gain the OccupancyChanged event

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

- **Exported motion sensors now emit Matter's `OccupancyChanged` event, not
  just the `occupancy` attribute** (issue #276). Apple reads the attribute
  directly and always showed the right value; #276's instrumentation points
  to Alexa consuming the event instead of polling the attribute, and the
  bridge never enabled the Occupancy Sensing cluster's `OccupancyEvent`
  feature, so a bridged motion sensor's attribute reports went out but
  nothing downstream ever moved there. The cluster now declares
  `OccupancyEvent` alongside `PassiveInfrared`, which is enough for
  matter.js to emit the event itself on every change — no other behaviour
  changes.

  **Live-verified on the reference rig 2026-08-22** — with the event
  enabled, motion now renders in the Alexa app, which it never did before.
  (A directly-commissioned occupancy sensor without this feature — an Aqara
  FP300 on its own fabric — already updates live in Alexa from the
  attribute alone, so Alexa reads attributes on devices it commissions
  itself; it drives *bridged* sensor state from the event instead, which is
  exactly the gap this closes.)

## 2026.24.2 — a half-edited device now says so

- **A Matter device made invisible by Indigo's "Edit Device Type" menu is now
  named in the log at every reconcile** (issue #62). Indigo changes a device's
  type the instant you pick it — before any save, and even if you cancel — and
  a device left that way is marked *not configured*, which hides it from the
  plugin completely: no updates, no commands, while it still holds its name.
  The plugin cannot refuse the change (it happens before any save) and cannot
  detect it the usual way (the hidden device is never started), so until now the
  only sign was an obscure name-collision warning, and then only if a reconcile
  happened to try recreating that exact endpoint. A device on an offline or
  decommissioned node produced no sign at all. It is now reported directly, by
  name, with the remedy: delete it and reload — the next reconcile recreates it.
- The install guide gains a section on this, and a standing warning not to use
  the Type menu on Matter devices. The Node-pin troubleshooting row is also
  corrected: that problem was fixed in 2026.24.1 and the guide still described
  it as open.

## 2026.24.1 — fans turn on

- **Turn On now works on fans that refuse the "On" mode** (issue #46). The
  plugin asked for a fan mode called "On" — which Matter deprecated in version
  1.2 and which appears in none of the speed lists a fan can declare. Older fans
  quietly treated it as "High"; newer ones reject it outright, and on those
  Indigo's Turn On did nothing at all. It now sets the speed to 100% instead,
  which every Matter fan accepts whatever modes it offers. Turn Off is unchanged
  — "Off" is the one mode every fan has.

  Setting a fan's brightness already worked and is untouched. Turn On means full
  speed rather than the speed it was last on; if you want a particular speed,
  set the brightness.
- **"Fan Always On" on a thermostat had the same problem**, and is fixed the
  same way — it now asks for "High", which every fan offers, instead of the
  deprecated "On".

  Both are **untested against real hardware** — there is no Matter fan on the
  development fabric — so if you own one, please report whether Turn On and Fan
  Always On behave.

## 2026.24.0 — momentary-only buttons work

- **A button that only reports "pressed" now reports it** (issue #231, reported
  by @coolcaper777 on an Aqara Light Switch H2). Its wired gang worked; both
  *wireless* gangs did nothing at all. Their Switch endpoints declare FeatureMap
  `0x02` — MomentarySwitch and nothing else — and Matter gates every Switch
  event on its feature, so those gangs emit `InitialPress` and no other event,
  ever. The plugin discarded `InitialPress` and waited for the release/long/
  multi-press event that tells one kind of press from another, which for these
  devices never comes: the Indigo device sat at its creation state for the life
  of the install. A switch declaring no release, long-press or multi-press
  feature now maps `InitialPress` to `shortPress` and increments `pressCount`,
  like any other button. Switches that *do* send a terminal event are unchanged
  — the leading edge is still discarded there, so a press is still counted once.
- **Existing button devices are healed at the next reconcile.** The capability
  is read from the endpoint's own FeatureMap and carried on the device; buttons
  created before this release pick it up automatically, with no need to delete
  and re-add them. If the FeatureMap later *changes* — a firmware update adding
  long-press to a gang, say — the device is corrected rather than left on the
  old answer.
- **A button whose device never reports its FeatureMap now says so.** Without
  that attribute the plugin cannot tell a momentary-only switch from a
  release-capable one, so it ignores the press — which looks exactly like the
  bug above. There is now one warning per such button in the event log, naming
  the device and what to send in if you hit it.

## 2026.23.10 — a pinned node is checked before it is trusted

- **A stale "Node bin directory" pin no longer shadows a newer node** (issue
  #101). The pin was honoured on an `npx`-exists check alone, and a successful
  install re-wrote it — so once a pin existed it kept itself alive. A user whose
  pin pointed at a leftover `/usr/local/bin/node` (an old Node.js `.pkg`, or
  Intel Homebrew on an Apple-Silicon Mac) could install a current node to fix
  the problem and have nothing change, because the pin still won; the only
  symptom was `matter-server connection lost … Connect call failed
  ('127.0.0.1', 5580)`. The pin is now checked before it is used: if the `node`
  beside it will not run, or reports a version below the required 22.13, the
  plugin says so — naming the pin, its version, and the node it was hiding —
  and falls back to auto-detect. Nothing is written to your prefs; the next
  successful *Install/update matter-server* re-pins to the node that ran it. A
  healthy pin (an nvm version directory, say) behaves exactly as before.
- **A rejected pin can no longer resolve straight back to itself.** The pin is
  usually one of the directories auto-detect checks first (that is where the
  install put it), so rejecting it used to walk into the same directory one step
  later — logging that the bad node had been ignored while going on to use it,
  and never reaching a healthy node further down the chain. When there genuinely
  is no alternative the pin is kept and the message says so, rather than
  claiming a fallback that did not happen.
- **The rejection message carries the real error.** "Bad CPU type in executable"
  or "Library not loaded: …" is the answer; it now appears in the log line
  instead of a flat "could not be run".
- The version check is bounded by a timeout, so a node on a stale network mount
  or a sleeping external disk cannot hang plugin startup.

## 2026.23.9 — the re-adopt picker leads with the likely answer

- **The re-adopt device picker now sorts its strongest candidates to the top**
  (issue #247). Selectable rows were alphabetical, which is scannable but says
  nothing about *which* row is the one you came for — on a large install the
  right device sat several screens down a list of every plug in the house. Two
  pieces of evidence already known to the dialog now rank it: a device whose
  name is the left-behind accessory's name (the same rule the bridge node's own
  "you could re-adopt this" nudge fires on), and a device that is already
  exported (`●`). Name match first, so the two together — the exact case the
  nudge reports — lead the list, then a name match, then any other exported
  device, then the alphabet as before. Nothing is hidden or dropped: selectable
  rows still come first and are still never truncated.

## 2026.23.8 — renaming, and what it does not do

Documentation only; no behaviour change.

- **Renaming an exported device in Indigo does not rename it in your
  ecosystem** (issue #221), and now both the Matter guide and the bridge field
  notes say so. Measured on 2026-08-19: the plugin *does* push the new name and
  the bridge records it within seconds — but Apple Home stores an accessory's
  name in your home, set when it was added, and a later change from the bridge
  does not override it. The Indigo name, and the optional *Name in ecosystems*
  field, decide what an accessory is called **when it first appears**, and
  nothing after that. To rename one you already have, rename it there.

## 2026.23.7 — the reboot leg is validated

Documentation only; no behaviour change.

The last outstanding leg of the export half's live validation is done. On
2026-08-19 the reference server was fully rebooted with **50 exported
accessories** paired into Apple Home. All 50 came back on the **same endpoint
numbers** under the same published identities; the bridge did not
re-commission itself (its install id and commissioning timestamp were
unchanged); both LaunchAgents started unattended; and the bridge was serving
**17 seconds** after the plugin started. Apple Home showed every accessory in
its own room with no duplicates and nothing unresponsive — including six that
had been *migrated*, and so publish an identity belonging to a different Indigo
device than the one driving them.

README, `MATTER.md` and `INSTALL.md` all carried the "still outstanding" note
and now record what was measured instead.

## 2026.23.6 — the documentation catches up with the sensors

Documentation only; no behaviour change.

### Changed

- **README** — the export section still said "the seven sensor types". There
  are now twelve, and they are named. It also gained the custom-state mapping,
  which had shipped without ever being mentioned to anyone reading the front
  page, and heat alarms among the roles deliberately not offered.
- **The Matter guide and the bridge field notes** — the mapping walkthrough
  still described a tick-box by its old name and its old meaning ("state is
  inverted", offered only for mapped devices). Rewritten for what the dialog
  does now: the role is suggested from the device's name, and *Reading is
  inverted* belongs to every binary sensor with contact sensors starting
  ticked. Both pages explain why that asymmetry exists — Indigo's on/off means
  *tripped*, Matter's contact means *closed* — and that a saved export is
  never re-derived, so a hand-corrected polarity stays corrected.
- **Heat alarms** are now in the "what cannot be exported" table rather than
  only in the prose above it.

## 2026.23.5 — contact sensors read the right way round

### Fixed

- **A contact sensor exported as itself reported every closed door as open.**
  Indigo's on/off state means *tripped* for every binary sensor — motion seen,
  leak seen, door OPEN. Matter agrees for all of them but one: its contact
  sensor reads true as **closed**. So exporting a Z-Wave door sensor straight
  through inverted it, and there was no way to correct it — the polarity
  tick-box only appeared for devices needing a state mapping, which an
  ordinary sensor does not.

  The tick-box now belongs to **every** sensor role that publishes a boolean,
  and a new contact export starts with it **ticked**, which is right for the
  common device. Change the role and it re-derives, because the same boolean
  means something different under a different role. Untick it and that is the
  last word — and an export you have already saved is never re-derived, so a
  polarity you corrected by hand stays corrected.

  **Existing exports are unchanged.** If you exported a contact sensor before
  this and its state reads backwards in your ecosystem, open it in
  *Manage Matter Exports…* and tick *Reading is inverted*.

## 2026.23.4 — the suggested role actually appears

### Fixed

- **Exporting a mapped device left the role blank, so every zone of a panel
  had to have its role set by hand.** The dialog pre-selects the state to read
  — from a sibling device of the same type where one exists — and the callback
  that suggests a role only fires when you *change* that menu. Having chosen
  for you, it never fired. The role is now suggested at the same moment the
  state is, and the status line says what it guessed and that you can change
  it. This is the whole point of the name heuristic, and on the path where it
  matters most it was doing nothing visible.
- **Zones named for more than one thing now read correctly.** "Patio Doors"
  and "Garden Doors" — ordinary door contacts — matched no needle and fell
  through to occupancy, because the needles were singular. Names of *things*
  are now matched in the plural too (doors, windows, gates, contacts,
  skylights); names of *phenomena* stay singular, since nobody calls a sensor
  "motions". The word-boundary anchors are unchanged, so "Drainage" still does
  not read as rain and "Defrost" still does not read as frost.

## 2026.23.3 — migrate onto a device you have already mapped

### Fixed

- **A device you had already exported with a state mapping was still refused
  as a migrate or re-adopt target**, telling you to do the thing you had just
  done. Both dialogs asked the classifier about the device *bare*, ignoring
  the mapping saved in its own export — so a Texecom zone exported five
  minutes earlier read back as one still needing a mapping. They now ask about
  the device **as configured**.
- **Migrating an accessory onto a mapped device would have left it dark.** The
  new entry took its options from the accessory's old device, so migrating
  from a Masquerade proxy — which needs no mapping, having a real on/off state
  — onto an alarm zone produced an entry with no mapping for a device that
  requires one: nothing would have been published, and the accessory would
  have gone quiet rather than moved. A state mapping describes *the hardware*,
  not the accessory riding on it, so it now comes from the device being
  migrated **to**. The mirror case is fixed with it: migrating off a mapped
  zone onto ordinary hardware no longer carries a state key the new device
  never publishes. Re-adopt already worked this way; migrate now agrees.

## 2026.23.2 — mappable devices come back to the migrate and re-adopt pickers

### Fixed

- **A device needing a state mapping vanished from "Migrate an exported
  accessory…" and "Re-adopt a Matter accessory…"** — no row, no reason, no
  clue (issue #252 follow-up, reported live). 2026.23.0 gave the classifier a
  third answer for devices Indigo does not type, and these two dialogs still
  asked their question the old way, so every such device — all 24 Texecom
  zones on the reference system — silently disappeared from both lists rather
  than being shown with a reason.

  They now appear, unselectable, saying *needs a state mapping first; export
  it directly, then retry*. That is the honest answer: inheriting an accessory
  never asks which state is the reading, so a migration onto an unmapped
  device would leave the accessory publishing nothing — dark rather than
  moved. Export the device directly first, which is where the mapping is
  asked, and it becomes an ordinary target afterwards. The same wording now
  appears if the migration is attempted anyway.
- **Both device pickers are now in alphabetical order.** They followed
  Indigo's own device order, which is not alphabetical — it collates
  punctuation Finder-style, so "Apple TV Power Socket" came before
  "AP_78:8a:20:b3:cc:4f" — and the list is built as two runs, selectable
  devices then explained ones. Across several hundred devices that reads as an
  alphabet restarting halfway down. Each block is now sorted A→Z on its own,
  so the devices you can pick still come first and never get truncated, but
  both halves are scannable.

## 2026.23.1 — the bridge node for the new sensor roles

The Node half of 2026.23.0's five new sensor roles — water leak, freeze, rain,
smoke and carbon monoxide. Pins `indigo-matter-bridge@0.13.0`; update the
plugin, then run *Plugins ▸ Matter ▸ Install/update the Matter bridge*.

Take the pair together. The older node does not know those roles, and refuses
an unknown role for the **whole** attach rather than the one endpoint — so
exporting a leak sensor against it would quieten every export, not just the
new one. Recovering is only this update: nothing is un-paired and no accessory
is renumbered.

## 2026.23.0 — export every sensor you actually own

The sensor half of the export bridge, widened. Five new roles, an on/off
default that reads the device's name instead of always guessing occupancy, and
— the big one — devices whose plugin keeps its reading in a state of its own
can now be exported at all. On the database this was developed against that
last change moves 229 devices out of "no resolvable Matter role", which was
the largest exclusion bucket in the product by a factor of ten.

### Added

- **Export devices whose reading lives in a plugin-named state** (issue #252).
  Alarm-panel zones are the case that prompted this: a plugin defining its
  devices as `custom` gives Indigo no on/off flag to read, so every zone —
  ordinary PIRs, door contacts, smoke and CO detectors — was listed as having
  no resolvable Matter role. The export dialog now asks **which state is the
  reading**, offering the boolean states the device actually publishes, with a
  tick-box for a state that is true when *nothing* is detected. Map the first
  zone of a panel and every other device of the same type pre-selects the same
  state, so the rest need only their role. If the plugin later renames or drops
  that state, the export stops publishing and says so in the log rather than
  reporting a fabricated "nothing detected". Only on/off states are mappable in
  this version; a device whose only readings are numbers says so in the picker.
  See ADR-0012.
- **Smoke alarm and carbon monoxide alarm export roles** (issue #179). Two
  roles, not one combined accessory: Matter gives both one device type and
  selects the sensing half by feature, while an Indigo sensor is a single
  on/off reading meaning a single thing — exporting a smoke-only sensor as
  both would leave it permanently reporting a CO reading nothing measured.
  Read-only, like every other exported sensor; Matter makes the self-test
  command optional and the plugin has nothing to run. Heat alarms are
  deliberately not offered a role — Matter's alarm type senses smoke and CO
  only, and an ecosystem automating on a "smoke alarm" that cannot detect
  smoke is worse than no export.
- **Water leak, freeze and rain export roles** (issue #236). An Indigo leak
  sensor exports as Matter's own Water Leak Detector, so Apple Home shows it
  as a leak sensor with leak alerts instead of forcing it out as an occupancy
  tile; freeze and rain sensors get the adjacent Matter types. All three are
  read-only, like every other exported sensor.

### Changed

- **The occupancy export role is now labelled "Occupancy sensor
  (PIR/motion)"** (issue #252). Matter 1.x has no motion device type —
  Occupancy Sensor is the standard's own representation of a PIR — so an
  Indigo motion sensor already exported correctly, but the picker offered no
  word a user searching for "motion" would recognise, and the absence read as
  "this sensor cannot be exported". Nothing about the exported accessory
  changes; this is the label, plus the ceiling written down in MATTER.md and
  the bridge field notes.
- **The default role for an on/off sensor is now read from its name** (issues
  #236/#252). A binary sensor publishes one boolean and no unit, so its name
  is the only evidence about what it detects — "Utility Leak Sensor" now
  defaults to Water leak, "Front Door" to Contact, "Study PIR" to Occupancy,
  and anything unrecognised still falls back to Occupancy as before. It
  remains a default only: all five binary roles stay selectable in the picker.
  This matters more than it used to, because changing a role after export
  costs the accessory's room in every paired ecosystem.

## 2026.22.1 — the bridge node for migrate

The Node half of 2026.22.0. Install it with
*Plugins ▸ Matter ▸ Install/update the Matter bridge* — the plugin pins
`indigo-matter-bridge@0.12.0`, and the migrate action needs the rekey the node
release carries. Nothing else changed.

## 2026.22.0 — Migrate an exported accessory…

### Added

- **`Plugin ▸ Migrate an exported accessory…`** (issue #246) — moves a live
  export onto a **different Indigo device**, for the case that turns out to be
  the common one: a thing in your house getting replaced while both Indigo
  devices exist at once. A socket swapped for a new model, a light moved from
  Zigbee to Matter. Pick the export, pick the device that drives it now, and
  the accessory's room, name, scenes and automations survive **by
  construction** in every paired ecosystem — not restored afterwards, but never
  disturbed, because nothing is removed and re-added and no ecosystem ever sees
  a change. This is the difference between migrate and re-adopt: re-adopt
  rescues an accessory whose device is already gone, and by then Apple has
  purged the room within seconds, so it lands in the Default Room. Migrate,
  used before you delete the old device, keeps everything. The role must match
  — a migration cannot also change what the accessory is. See ADR-0011.

### Changed

- **Re-adopt's dialog, log and documentation stopped overclaiming.** They
  implied the room and name came back with the accessory; measured on live
  hardware, they do not — only the accessory number, absence of duplicates and
  correct control routing are restored. The wording now says so, and points at
  migrate for the case where the room really can be kept.

## 2026.21.1 — the bridge node for protocol v2

The Node half of 2026.21.0, pinned to `indigo-matter-bridge@0.11.0`. Exports
stay offline until it is installed via
*Plugins ▸ Matter ▸ Install/update the Matter bridge* — version skew fails
closed by design, and restarting the bridge agent does not help, because
launchd relaunches the same node.

## 2026.21.0 — Re-adopt a Matter accessory…, and role changes cost the room on purpose

> **This release bumps the bridge protocol to v2 — Matter export stops until
> the paired bridge-node release is installed via
> *Plugins ▸ Matter ▸ Install/update the Matter bridge*.** Version skew fails
> closed by design: nothing is un-paired and nothing is renumbered, but no
> accessory is served until the node matches. Restarting the bridge agent does
> not help — launchd relaunches the same node.

### Added

- **`Plugin ▸ Re-adopt a Matter accessory…`** (issue #219) — hands a
  left-behind accessory identity to a different Indigo device. An accessory
  is left behind when the Indigo device that exported it is deleted, or when
  it stops being exported; the room, name, scenes and automations built on it
  in Apple Home and every other paired ecosystem survive untouched, because
  nothing is re-paired and nothing is renumbered. Only works when the
  replacement device can take the same role as the accessory it is
  inheriting — cross-role re-adopt is refused, since that is exactly the
  wedge this release's role-change fix exists to eliminate. **A role change
  is NOT recoverable this way**: its old accessory number is retired on
  purpose (see Changed, below), so those accessories are not offered. An
  accessory un-exported by a plugin older than 2026.16.2 has no role/name on
  record and cannot be re-adopted either; export the device normally
  instead.

### Changed

- **Changing an export's role now costs the room, on purpose** (issue #240).
  Previously an in-place role change sometimes left Apple Home stuck on
  "could not change settings" until the home hub was restarted, because a
  cached accessory structure changed under a Matter endpoint number a
  controller believed was stable. A role change now removes the old
  accessory from every ecosystem and adds a new one under a fresh number —
  one deliberate re-room every time, predictably, instead of an occasional
  wedge. `Manage Matter Exports…` states this before you confirm. If you are
  already stuck from an older version, the hub-restart recipe in
  `docs/MATTER.md` still applies as a one-time recovery.

## 2026.20.0 — colour-while-off forwards, like brightness

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Changed

- **Picking a colour or colour temperature on a lamp that is showing off now
  reaches Indigo** (issue #235) — the same rule brightness follows: the
  bridge sends, Indigo decides. On many RGB lamps a colour write lights the
  lamp at the new colour (the device driver's own behaviour); on lamps with a
  white channel a colour-temperature change is normally stored without
  turning the lamp on (an RGB-only lamp refuses it with a logged reason).
  Previously direct colour commands were dropped while the accessory was off
  (scene recalls always went through).

## 2026.19.0 — exported brightness and colour wait for Indigo too

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Changed

- **Exported dimmers and colour lights no longer move optimistically**
  (issue #143, the LevelControl/ColorControl half — the onOff half shipped
  in 2026.17.0). Dragging the brightness slider, or picking a new colour or
  colour temperature, in Apple Home/Google/Alexa sends the command to Indigo
  and the control springs back only once Indigo reports the real level or
  colour — the same rule the on/off tile, locks and covers already followed.
  Commands while the accessory is showing off forward too — brightness turns
  the light on to that level (Indigo's own semantics); colour originally
  stayed gated in this version and was aligned with brightness in 2026.20.0
  (issue #235), which is the behaviour that actually ships.
  **No accessory is re-created and no room assignment is lost by this
  release** — unlike 2026.18.0's battery migration, this is a behaviour
  change only: the exported accessory's identity, name and room stay exactly
  as they were.
- **CIE xy colour commands now reach Indigo too.** An ecosystem driving an
  exported colour light in its xy representation used to be silently
  ignored; it now reports the equivalent hue/saturation, converted through
  the cluster's own conversion utility for that direction.

### Fixed

- **`extendedColorLight` accessories gain the `RemainingTime` attribute
  their device type mandates** (conformance fix, HR-1) — attribute addition
  only, no accessory recreation.
- **A dimmer command carrying `WithOnOff` can no longer strand a running
  ecosystem "on for N minutes" countdown** — closes the remaining half of
  issue #201's ghost-timer defect: the coupling that used to move the on/off
  state directly, ahead of Indigo's confirmation, is now itself invocation-
  driven.

## 2026.18.0 — battery level on exported accessories

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Added

- **Battery level now appears on exported accessories that report one**
  (issue #220) — automatic, no export setting to change. A device's own
  `batteryLevel` state is the whole trigger: the plugin declares a
  `PowerSource` cluster the first time it sees one, and every ecosystem then
  shows the reading on its normal update cadence. A device that has never
  polled (Indigo's own `0` at device creation) does not trigger a false
  "battery critical" alarm. **Migration note:** on the first reconnect after
  this update, every already-exported battery-bearing device is re-created
  once — endpoint identity is preserved, but names/rooms assigned in paired
  ecosystems may need re-assigning.

## 2026.17.0 — exported switches, plugs and lights wait for Indigo

> Takes effect on a live install only once the paired bridge-node release
> ships; the plugin pins the bridge node by exact version.

### Changed

- **Exported on/off accessories no longer move optimistically** (issue #143,
  onOff half). Tapping a tile in Apple Home/Google/Alexa sends the command to
  Indigo and the tile confirms only when Indigo reports the device's real
  state — the same rule locks and covers already followed. A command that
  matches the accessory's current state is now forwarded instead of silently
  dropped.

### Fixed

- **Turning a device off in Indigo now cancels a running ecosystem
  "on for N minutes" countdown** (issue #201) — previously the countdown
  fired late and switched off a device that was already off (or had been
  turned back on since).

## 2026.16.2 — export conformance guards and port-bind verification

### Fixed

- **A managed matter-server or bridge node that lost its port-bind race right
  after bootstrap is now detected and reported** (issue #187) — previously
  the bridge node was never checked for port conflicts at all.
- **Un-exporting a device no longer destroys the evidence a future re-adopt
  needs** (issue #219 groundwork) — the endpoint map keeps the role/label and
  marks the record orphaned instead.

### Changed

- Warnings added for the two Alexa-relevant configurations: a non-default
  Matter port, and export counts past the community-reported ~50-endpoint
  ceiling. The full Alexa conformance sweep (issue #222) is documented in
  `docs/MATTER.md`.

## 2026.16.1 — manual commission now logs what happened

### Fixed

- **A manual commission that failed via a matter-server protocol error or a
  commissioning error logged nothing at all** (issue #227) — the reason was
  stored on the job for a poller that doesn't exist on the menu path, so a
  failed commission was indistinguishable from a silently successful one.
  `_fail()` now logs an ERROR line naming the device and the reason the
  moment it happens.

### Changed

- **Manual commission now narrates what it's doing, in the Event Log**
  (issue #226) — the raw `manual commission → 202 {'jobId': …}`
  acknowledgment read like an error to anyone unfamiliar with the wire
  format, and success logged nothing tied to the commission at all. It now
  reads:
  - Accepted: `Commissioning "<name>" — this takes up to a minute; the
    result will be logged here.`
  - Already running: `A commission for this setup code is already
    running — its result will be logged here.`
  - Succeeded: `Commissioned "<name>" → node <id>, created N Indigo
    device(s).`
  - Failed: the #227 ERROR line above.
  - Timed out: the existing warning, now led by the device's name (job id
    kept in parens), and — for the "matter-server gave up but the device
    may still join" case — extended with the most common real-world
    causes (a pasted original QR code instead of a fresh share code,
    an expired share window, or the device not being IP-reachable from
    this Mac).

  The raw wire acknowledgment (202/409) is unchanged in substance but now
  logs at DEBUG instead of INFO.

## 2026.16.0 — share an Indigo-commissioned device with another ecosystem

### Added

- **"Share a Matter device with another ecosystem…"** menu item (issue
  #210) — opens a fresh commissioning window on a device Indigo already
  controls, so a second ecosystem (Apple Home, Alexa, Google, Home
  Assistant) can add it too without you finding the device's own pairing
  mode. The reverse of the share model this plugin already uses to join
  devices commissioned elsewhere. Codes go to the Event Log, same reason
  the export bridge's pairing codes do — Indigo dialogs have no dynamic
  labels. Warns (never blocks) when a device's fabric capacity is getting
  tight; an Apple Home pairing alone uses two slots.
- **"Share this device with another ecosystem"** device action on the
  Matter Node device type — the same feature, reachable from a control
  page, trigger, or action group.

## 2026.15.0 — every Matter node gets an Indigo device

**Read the upgrade notes below before installing: this release renames some
existing devices and adds one device per Matter node.**

Matter describes a device as a *node* made of *endpoints*, with endpoint 0 as
the node's own "read me first" endpoint. This plugin has always created one
Indigo device per endpoint and nothing for endpoint 0 — so everything the node
itself carries (its identity, firmware version, reachability, battery, and on
some hardware its energy metering) had nowhere to live. Every other Matter
controller — Home Assistant, SmartThings, Hubitat, openHAB, Apple Home, Alexa
— has a node-level object; this plugin was the outlier. It does now.

### Upgrade notes — what changes on an existing install

Nothing you must do, but three visible changes, on the first reconcile after
upgrading:

1. **A new device per Matter node.** Named after the family (an Aqara FP300
   called "Landing sensor" gets a "Landing sensor" node device beside its
   "Landing sensor - Motion" and the rest). It carries the node's name,
   firmware version, reachability and — where the hardware reports one —
   battery level.

2. **Some devices are renamed.** Only devices still wearing a name *this
   plugin generated* are touched; **a device you renamed yourself is never
   renamed**. In practice the ones that change are single-endpoint devices,
   which gain their role suffix so the family reads consistently:
   `Office Plug` becomes `Office Plug - Switch`, and the bare `Office Plug`
   becomes the node device. Devices named after the product where the family
   has a better name also true up (`ALPSTUGA air quality monitor - Switch` →
   `Bedroom air quality monitor - Switch`).

   **Triggers, schedules, action groups and control pages are unaffected** —
   Indigo binds those to a device's *id*, not its name. What a rename does
   break is anything that looks a device up **by name**: Python scripts using
   `indigo.devices["Office Plug"]`, and any external integration keyed on the
   name rather than the id. If you have those, check them after upgrading.

3. **A node's devices are grouped together** in the device list, and node
   devices are filed alongside their siblings.

   ⚠️ **New hazard that comes with grouping:** deleting a grouped device in
   Indigo's UI now offers to delete **every device in that family**, and if
   you confirm, the plugin recreates the endpoint devices with **new ids** —
   so anything bound to the old ids stops working, silently. To hide a device
   you do not want, move it to a folder you do not look at. To remove the
   hardware, **decommission the node** instead. See `docs/MATTER.md`.

Battery routing also becomes evidence-based (below). On hardware whose power
source declares which endpoints it actually powers, a device that previously
showed a battery level may stop being fed one — the plugin logs a one-time
line naming any device this affects, so it is discoverable rather than silent.

### Added

- **A `Matter Node` device per node** (issue #204, ADR-0008), created only
  where the node's own attribute list evidences it. Carries `nodeLabel`,
  `softwareVersion` (parsed since the first release and never displayed until
  now), `reachable`, and `batteryLevel` where the node reports one.
- **Node-level energy** (issue #204): energy clusters on endpoint 0 are no
  longer dropped. Where the node says which endpoint they measure, the reading
  joins that device; where it cannot be attributed, it lands on the node
  device.
- **Grouping** (ADR-0009): a node's devices are grouped, so the family shows
  together and Indigo reads battery from the group's leading device.
- **"Recreate Matter node devices…"** menu item — the way back from deleting a
  node device, which is otherwise honoured permanently.
- **"Report this node's settable attributes"** device action, and the last
  survey summary on the node device's Edit dialog.

### Changed

- **`PowerSource.EndpointList` is now the authority for battery routing**
  (issue #205). The spec has always said which endpoints a power source
  powers; the plugin used to reconstruct that with a heuristic in four
  separate places, and issue #82 was a bug in one of them. Firmware too old to
  report it keeps the old heuristic, per source.
- Devices recreated after a deletion take their family's name rather than the
  product name.
- Node-level settings (the localization clusters) remain unimplemented: no
  device on the validation fabric reports them, and shipping a settings screen
  no hardware could exercise is how issues #190 and #197 happened. Tracked as
  issue #212.

### Fixed

- Battery readings can no longer cross-contaminate between the children of a
  bridge (the conditions for issue #82 are gone).
- A decommission no longer leaves a node unable to get a node device if it is
  later recommissioned onto the same node id.
- Meter links survive the empty attribute snapshot that follows a
  matter-server restart, so a node device cannot latch a duplicate power
  reading.
