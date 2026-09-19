---
parent: Decisions
nav_order: 17
title: "ADR-0017: currentLevel while off is the last confirmed on-level, not the Lighting minimum"

status: "accepted"
date: 2026-09-19
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0017: currentLevel while off is the last confirmed on-level, not the Lighting minimum

## Context and Problem Statement

Indigo reports brightness 0 whenever a dimmer is off, so the plugin's off push
has always carried `level: 0`, and the bridge node wrote that straight through
as Matter `currentLevel: 1` (the Lighting-feature minimum) — an off light has
always advertised the minimum level on the fabric.

Issue #353: over Matter, "Alexa, turn on `<dimmer>`" drove an exported
dimmer that was off to 100%, overriding the dimmer's own on-level. On the wire, the Amazon
fabric (vendor 4631) sends `onOff.on` and then, tens of milliseconds later
(40–85 ms observed), a SEPARATE plain `moveToLevel` command — `level: 254`,
`transitionTime: 0`, not the `WithOnOff` variant Apple Home was observed
using for its own brightness control (nothing observed shows Apple sending a
level command for a plain turn-on). Reproduced against both a Z-Wave dimmer
and a Zigbee dimmer bridged from this plugin, so the wire behaviour is
independent of the radio — it is not a Z-Wave-only or Zigbee-only quirk. That
is narrower than "role-wide": both live dimmers tested are `dimmableLight`;
no colour-temperature or colour light was live-tested, so whether Alexa does
the same thing to `colorTemperatureLight` or `extendedColorLight` is inferred
from the shared code path, not measured. That plain command is forwarded
while the light is off because the node seeds the LevelControl
`options.executeIfOff = true` on purpose (Simon's #235 ruling, recorded in
`bridge-node/src/endpoints.ts`: setting a level on an off device means "turn
on at that level", so the bridge sends and Indigo decides) — so the question
is not whether to forward the command, but what `currentLevel` reads while
off, which is what gives the command something to act on.

**Reporting the minimum while off was the actual deviation from the
reference implementation, not the other way round.** Reading
`project-chip/connectedhomeip`'s `src/app/clusters/level-control/codegen/
level-control.cpp` (`emberAfOnOffClusterLevelControlEffectCallback`): on
`Off`, the reference implementation fades `CurrentLevel` to the minimum over
`OnOffTransitionTime` — but only as a transient step. When `OnLevel` is not
defined — this bridge's case, on every role: `onLevel` is null throughout —
the same callback then writes the pre-off level back into `CurrentLevel` at
the end of that transition (`CurrentLevel::Set(endpoint, storedLevel)`), in a
branch whose comment quotes the spec sentence it is implementing: "If
OnLevel is not defined, set the CurrentLevel to the stored level." That
stored level is an in-memory field the callback keeps, not a second
attribute. Only when `OnLevel` IS defined does the reference implementation
leave `CurrentLevel` parked at the minimum while off. So a reference-
implementation light with a null `OnLevel` — this bridge's exact
configuration — reads its last level while off, not the minimum; this
bridge, by writing the minimum on every off push, was the implementation
diverging from the reference, and this decision brings it into line rather
than choosing to depart from it further. (This reading is of the reference
implementation's source and the spec sentence quoted in its own comments;
the Matter Level Control cluster specification document itself was not
consulted directly, so this is not a claim that reporting the minimum was a
spec violation — only that it was not reference-conformant.)

## Decision Drivers

* The **#143/#201 confirmed-truth doctrine** (ADR-0013's own founding
  constraint): an ecosystem must never be told a state Indigo has not
  confirmed. Any fix here has to hold that line, not carve a new exception
  into it — `onOff`, `level` for every OTHER combination, `hue`, setpoints,
  and every other command must be untouched.
* The failure is real load on the user, not a log annoyance: an
  Alexa-driven "turn on" of an exported dimmer that was off landed at 100%,
  independent of the dimmer's own configured on-level.
* Whatever fixes it must not touch `executeIfOff`, any protocol frame, or the
  plugin (Python) side — Indigo already does the right thing (reports
  brightness 0 while off, turn-on-to-level semantics when a level is pushed
  to an off device); the bridge node is where the minimum gets written, so
  the bridge node is where this is fixed.
* "Alexa, set X to 70%" sent to an off light produces the identical
  On + plain-MoveToLevel wire shape as "Alexa, turn on X" — any fix that
  gates or drops the second command breaks the first as collateral damage.

## Considered Options

* **A — retain the last confirmed non-zero level in `currentLevel` instead
  of writing the Lighting minimum whenever a pushed `level` reads 0.**
  Chosen. Two ways of scoping the withheld write were built and compared —
  see Decision Outcome.
* **B — seed `executeIfOff: false`** instead of `true`, so the plain
  `moveToLevel` is silently dropped by matter.js before this bridge ever
  sees it.
* **C — a timing heuristic:** drop a `moveToLevel(max)` arriving within some
  window (e.g. <200ms) of `onOff.on`, the shape the `RiDDiX/home-assistant-
  matter-hub` fork uses.
* **D — a per-export "on level" option**, configured by the user alongside
  each dimmer's export settings, duplicating the dimmer's own on-level (or
  Indigo's) as a second source of truth the node would apply itself.

## Decision Outcome

Chosen option: **A**, in the form of rule **a** below. `retainLevelWhileOff()`,
called from `applyStates` after the refusal decision (so a lawful push is
still counted as consumed before this function strips anything), deletes a
`currentLevel` write the role's own `levelPatch` already produced whenever
the push itself carries `level: 0` — full stop. **A pushed `level` of 0
never writes `currentLevel`.** There is no attribute read and no "off" test:
it does not matter what `onOff` the same push carries, or what the
endpoint's `OnOff` attribute currently reads. When the write is stripped,
the endpoint's `currentLevel` simply keeps whatever value it already held —
the last non-zero level Indigo confirmed.

This has to live in `retainLevelWhileOff`, downstream of where `levelPatch`
has already produced its write, and not inside `levelPatch` itself:
`statePatchOrRefuse` refuses a push only when its ENTIRE resulting patch
comes out empty. Folding the level-0 suppression into `levelPatch` would
make a bare `{level: 0}` push — no `onOff` key, nothing else in it —
resolve to an empty patch and come back `malformed_args`. The ordinary off
shape, `{onOff: false, level: 0}`, still has `onOff` in its patch regardless
of where the level-0 rule runs, so that push is never at risk of being
refused either way. Stripping the level write downstream, after the refusal
decision, is what lets a bare `{level: 0}` push be counted as consumed and
then become a lawful no-op, instead of being refused. `levelPatch` also has
no endpoint in hand, which mattered for rule b below and is a further reason
the rule was never written there.

This is bridge-node-only: no plugin change, no protocol frame change,
`executeIfOff` stays `true` — the plain-`moveToLevel`-from-off command is
still forwarded exactly as before; what changes is only that an off light
stops advertising the Lighting minimum. Applies to the three level-bearing
roles: `dimmableLight`, `colorTemperatureLight`, `extendedColorLight`.

This is squarely inside the #143/#201 doctrine, not an exception to it: the
value retained in `currentLevel` while a pushed level reads 0 is not
invented. It is the last non-zero level Indigo itself confirmed — the same
confirmed-truth standard every other attribute in `endpoints.ts` is held to.
What changes is only *which* confirmed value `currentLevel` represents at
that moment: previously the (also confirmed, by construction) Lighting
minimum; now the last confirmed non-zero level. `onOff: false` still reports
truthfully and unconditionally the instant Indigo confirms it — this ADR
does not touch that attribute or when it moves, and this rule does not
consult it to decide anything about `currentLevel` either.

**Rejected variant, considered and built first: rule b.** Withhold the
write only when the device is also off — the same push carries
`onOff: false`, or carries no `onOff` key and the endpoint's `OnOff`
attribute already reads `false` — on the reasoning that a device reporting
on at level 0 should still float to the minimum rather than have that
hidden. Rule b was implemented first, passed review for correctness under
the confirmed-truth doctrine, and is the build the live A/B in Confirmation
below was run against. It was replaced by rule a on two grounds:

1. The owner confirms brightness 0 always means off in Indigo, so "on at
   level 0" is not a state Indigo ever actually produces — the attribute-read
   branch in rule b guards against a case that cannot occur, not a real one.
2. Under the Lighting feature `currentLevel` cannot represent 0 at all — the
   minimum is 1 — so writing 1 for a pushed 0 was already a fudge, and
   carrying "off" truthfully was `onOff`'s job all along, not `currentLevel`'s.

Rule b was also MEASURED to re-open #353 for a push sequence with no
coalescing: the plugin pushes one diff per callback with no batching of its
own, so frame shape depends on how the owning Indigo plugin happens to
group its state updates. A split off-push arriving level-first —
`{onOff: true, level: 20}`, then `{level: 0}` (rule b still sees the
attribute reading on, so it writes 1), then `{onOff: false}` — ends at
`currentLevel: 1`, the pre-fix state, even though the device genuinely went
off, just across two frames instead of one. The live A/B below happened to
send both dimmers' off events as a single frame each (one Z-Wave, one
Zigbee) — rule b passed that test, but that is a property of those two
events, not a guarantee for every Indigo plugin's push shape. Rule a is
immune to frame order because it never inspects `onOff` at all.

Accepted cost of rule a, beyond the known limits below: if a device ever did
report on with brightness 0, the ecosystem would show it on at its retained
level rather than at the minimum — judged unreachable, per the owner's
confirmation above.

### Consequences

* Good, because the measured #353 failure — an Alexa turn-on from off
  landing at 100% — stops in practice. Observed live: when the endpoint
  advertises a retained non-minimum level, Alexa does not send the
  follow-up plain `moveToLevel` at all (five turn-ons, zero overrides — see
  Confirmation), matching upstream's own inference that Alexa treats an
  already-non-minimum level as nothing to correct
  (`home-assistant-matter-hub#880`) — though that motive is upstream's
  guess, not something this bridge observes directly. Should a plain
  `moveToLevel` arrive anyway, it is still forwarded exactly as before —
  `executeIfOff` is untouched — so nothing here depends on blocking or
  predicting the command, only on no longer advertising the minimum while
  off.
* Good, because it holds the confirmed-truth doctrine exactly as tightly as
  before for every other key and command — this touches one attribute, one
  narrow condition (a pushed `level: 0`), nothing else.
* Bad, because there is no level history: ANY accessory whose stored
  `currentLevel` is still the Lighting minimum — a brand-new export's
  first-ever construction push, or a dimmer that was already off (and so
  already parked at the minimum) when this fix was installed — advertises
  the minimum and can still be driven to 100% on its first Alexa turn-on,
  exactly as before this fix. (The Confirmation control arm below is
  exactly such a pre-existing, pre-fix-parked accessory.) On a fresh
  construction push, the `1` comes from the construction patch itself
  computing `percentToCurrentLevel(0)`, not from a default being "kept".
* Good, because an existing accessory keeps its level across a node
  *restart*: `restoreEndpoints()` rebuilds the map's restorable entries
  before attach, matter.js restores the persisted (nonvolatile)
  `currentLevel`, and the attach that follows takes the `update` branch,
  where `retainLevelWhileOff` withholds the `level: 0` write rather than
  flooring the restored value again. Both halves matter together —
  persistence alone would be floored right back down by the attach without
  this function. Pinned by `bridge-node/test/restore.test.ts`, describe
  "issue #353: currentLevel retention over a REAL node restart".
* Bad, because that restart-survival does not extend to recreating an
  endpoint under a *different role*: doing so resets a retained level to
  the minimum while off (measured during review). Restart-survival holds
  only for the same role; a role-change recreate floors the retained level
  exactly like a brand-new export.
* Neutral, because after a rekey (ADR-0011 migrate) to a different Indigo
  device that is off, `currentLevel` keeps the PREVIOUS device's retained
  level until the new device's first turn-on — kept deliberately, not a gap
  to close. ADR-0011 treats a migrate as the same logical accessory, and
  resetting the level on rekey would expose the migrated accessory to
  exactly the first-turn-on 100% override this ADR exists to prevent. Said
  honestly: for the new physical device this is a level it never itself
  confirmed, retained on the strength of the old device's history; it heals
  the moment the new device is next turned on for real. Pinned by a test.
* Bad, because matter.js's scene recall compares an incoming level against
  the endpoint's current `currentLevel` and skips the level half of the
  recall whenever they already match — the level half of a scene recall is
  skipped because of that equality guard, not because of anything this ADR
  adds. A Matter scene recall to exactly the retained level, while the
  light is off, therefore emits no level command: a pre-existing matter.js
  quirk that was always true, newly reachable now that "off" and "the
  retained level" can coexist on the same attribute.
* Bad, because Alexa's own threshold for "minimum" versus "a normal level"
  is not fully known: live, the minimum drew the `moveToLevel(254)`
  override, and retained levels of 20%, 100%, and one wall-dimmed level did
  not — but nothing between the minimum and 20% has been tried.
* Bad, because while the light is off, a relative Matter `step` command now
  computes from the retained level rather than from the minimum — a
  behaviour change for that narrow case, in the same direction as the fix
  itself (it is no longer stepping from a value nobody chose). `move` is
  unaffected either way: it targets ±∞, which the bridge clamps, so it
  always lands at 0% or 100% regardless of what `currentLevel` held going
  in.
* Neutral, because this bends `BRIDGE_PROTOCOL.md` §4.2's earlier "0 ↔ off
  preserved exactly" statement for the outbound direction — amended in the
  same change as this ADR to describe the asymmetry precisely (inbound is
  unaffected; outbound no longer moves `currentLevel` to the minimum for a
  pushed `level: 0`).

### Confirmation

**Confirmed live, 2026-09-19, on the maintainer's system** — a patched
bridge node (published `0.17.3` and this repo's `main` differed only in
comments at that point, so the test build was `0.17.3` plus this change and
nothing else) running against a real Alexa fabric, with both arms of the
test captured from the SAME Alexa controller node, on the same build,
minutes apart. This A/B ran rule b, the build in place at the time, before
it was replaced by rule a above on the split-frame evidence; every case the
A/B exercised is one where rule a and rule b agree, because both off events
in the test arrived as a single frame carrying `onOff: false` — rule a does
not change the outcome for anything measured below, only for the
split-frame case rule b was found to mishandle. The evidence is the bridge
node's own log of inbound invocations:

* **Control arm — minimum advertised.** A Zigbee dimmer that had been off
  since before the patched node started, so still advertising the Lighting
  minimum (the fresh/no-history known limit above). A voice "turn on"
  produced `onOff.on` followed 85 ms later by a plain `moveToLevel`
  `level: 254`, and the plugin dispatched `setLevel 100`. This is #353
  reproduced, and it shows this controller node does send the second
  command when the level reads minimum.
* **Treatment arm — retained level advertised.** The same dimmer, dimmed at
  the wall, turned off, then a voice "turn on": `onOff.on` and nothing else;
  the light came back at its dimmed level. A Z-Wave dimmer, turned off with
  100% retained and again with 20% retained: `onOff.on` and nothing else, both
  times. Across the session, five turn-ons from off with a retained level and
  none followed by a `moveToLevel`, from the same controller node that sent
  the `254` in the control arm. Ordinary "set to N%" commands while on
  (`moveToLevel` 127 / 25 / 51) kept working.

Pre-fix log history agrees: from one Alexa controller node, every `on` sent
to a dimmer that was off was followed by `moveToLevel 254` (7 of 7 across the
two dimmers), and the `on`s sent while a dimmer was already on at a normal
level were not (0 of 2). What this does NOT establish: Alexa's threshold (the
lowest retained level tried was 20%, nothing between the minimum and 20% has
been tried), or why Alexa does it — that remains upstream's inference
(`home-assistant-matter-hub#880`), now consistent with two independent A/Bs.

**Re-checked live on the rule-a build: PENDING.**

In the suite, `retainLevelWhileOff` is covered by
`bridge-node/test/registry.test.ts` — retention in a single frame, a bare
`level: 0` push, replay, all three light roles, step-from-retained-level, the
`create`-with-persisted-history path, and the fresh-endpoint known limit —
and by `bridge-node/test/restore.test.ts` for a real node restart
(`restoreEndpoints` → attach → `update` → `applyStates`).

## Pros and Cons of the Options

### A — retain the last confirmed non-zero level

* Good, because it is entirely bridge-node-side: no plugin change, no
  protocol frame change, `executeIfOff` untouched.
* Good, because it stays inside the confirmed-truth doctrine — the retained
  value is Indigo-confirmed, just applied to a new moment (a pushed
  `level: 0`) rather than invented.
* Neutral, because it does not explain WHY Alexa sends the second command —
  it removes the thing that command has to act on, which is enough to fix
  the symptom without needing the why.
* Bad, because of the fresh-endpoint, role-change-recreate, and
  scene-recall known limits above.

### B — seed `executeIfOff: false`

* Good, because it is the smallest possible change — one seeded option flips.
* Bad, because `executeIfOff: false` is not a smaller, tweakable version of
  forwarding — it is exactly the choice upstream shipped and then had to
  reverse. HAMH #790/#603 are the bugs that setting caused upstream (a level
  command sent to an off light silently did nothing, so "Alexa, set X to
  70%" on an off light never worked); upstream's own fix, PR #855, is to
  turn `executeIfOff` ON, not to patch around it while it stays off.
  Rejected outright, not as "B plus a further change" — the further change
  upstream needed was to abandon B.

### C — a timing heuristic

* Good, because it is Alexa-fabric-scoped and does not touch `currentLevel`
  semantics at all.
* Bad, because it swallows a genuine "turn on at 100%" request that happens
  to arrive inside the window — the `RiDDiX/home-assistant-matter-hub` fork
  that uses this shape broke a Siri room-level "set to 100%" command (their
  issue #306: `On` + `moveToLevelWithOnOff(254)`, a similar shape and timing
  to the Alexa pair this ADR targets, not an identical command) and had to
  make the heuristic opt-in and fabric-scoped to ship it at all (#460).
* Bad, because it is the exact timing-heuristic shape this repo's ADR-0013
  already rejected as an option (there, for colour temperature) in favour of
  a push+tolerance mechanism — reintroducing it here for level would be
  inconsistent with that precedent.
* Bad, because it gates a command on believed state, against issue #235's
  standing rule in `endpoints.ts`: commands are forwarded, never gated on
  what the bridge believes — Indigo is the one system here that gets to
  decide what a command means.

### D — a per-export "on level" option

* Good, because it gives the user an explicit, visible control.
* Bad, because it does not stop the 254 command arriving after `onOff.on` —
  it only changes what the node would apply if it chose to intercept that
  command, which this repo's #235 rule says it should not.
* Bad, because it duplicates the dimmer's own on-level (or Indigo's) as a
  second source of truth the node would have to keep in sync — exactly the
  kind of state this bridge otherwise avoids owning.

## More Information

Issue #353. Builds on the #143/#201 confirmed-truth doctrine and on
ADR-0013's precedent for weighing a device/ecosystem-side clamp against that
doctrine (there: colour temperature; here: level while off). Related upstream
work: `t0bst4r/home-assistant-matter-hub#880` (the same bug, the A/B this ADR
cites as inference) and its fix `f761edd0`; `RiDDiX/home-assistant-matter-
hub`'s timing-heuristic fork (rejected shape, option C above). Amends
`docs/BRIDGE_PROTOCOL.md` §4.2's level/position bullet in the same change.
See also `docs/DEVICE-NOTES.md`'s `Alexa's Matter "turn on" for dimmable
lights (#353)` entry for the user-facing, sanitised write-up.

## For AI agents
- DO: keep `retainLevelWhileOff` scoped to exactly a pushed `level: 0` — do
  not extend it to other keys or other level values without a similarly
  measured failure.
- DO: keep `executeIfOff: true` and the plain-`moveToLevel`-while-off forward
  path untouched — the fix is what `currentLevel` reads, never whether the
  command reaches the override.
- DON'T: reintroduce an `onOff`/attribute gate on the withheld write
  (rule b) without re-reading the split-frame evidence above — it was
  built, passed review, and was replaced on measured grounds, not on a
  whim.
- DON'T: flip `executeIfOff` to `false` — see option B's rejection; it
  reopens the bugs (HAMH #790/#603) upstream already had to fix by turning
  execute-if-off ON.
- DON'T: move this suppression into `levelPatch` itself — see the
  `malformed_args` reasoning in Decision Outcome; it has to run in
  `retainLevelWhileOff`, after the refusal decision.
- DON'T: read the "Decision Outcome" here as license to push an unconfirmed
  value into `currentLevel` for any other case — the value retained here is
  always one Indigo itself already confirmed while the light was on.
- DON'T: re-propose option B, C, or D without new evidence — each was
  rejected against a specific, cited failure mode above, not on convenience.
