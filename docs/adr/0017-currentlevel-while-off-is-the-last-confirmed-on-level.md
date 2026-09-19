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
fabric (vendor 4631) sends `onOff.on` and then, 40–70ms later, a SEPARATE
plain `moveToLevel` command — `level: 254`, `transitionTime: 0`, not the
`WithOnOff` variant Apple Home was observed using for the same gesture.
Reproduced against both a Z-Wave dimmer and a Zigbee dimmer bridged from this
plugin, so the behaviour is role-wide, not Z-Wave-specific. That plain
command is forwarded while the light is off because the node seeds the
LevelControl `options.executeIfOff = true` on purpose (ADR-driven by Indigo's
own semantics: setting a level on an off device means "turn on at that
level") — so the question is not whether to forward the command, but what
`currentLevel` reads while off, which is what gives the command something to
act on.

**Reporting the minimum while off is not a Matter spec violation.** The
connectedhomeip reference implementation moves `CurrentLevel` to the minimum
on `Off` and keeps the pre-off level in a separate stored attribute a bridge
cannot advertise (there is no such second attribute on this cluster as this
bridge implements it). So this ADR is a deliberate, useful deviation in what
this bridge chooses to represent while a light is off — not a conformance
repair.

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

* **A — retain the last confirmed on-level in `currentLevel` while off, and
  stop writing the Lighting minimum there.** Chosen.
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

Chosen option: **A**. `retainLevelWhileOff()`, called from `applyStates`
after the refusal decision (so a lawful `{onOff:false, level:0}` push is
still counted as consumed before this function strips anything), deletes a
`currentLevel` write the role's own `levelPatch` already produced, and only
when three things all hold: the push carries `level: 0`; the resulting patch
would have written `currentLevel`; and the device is off — either because
this same push says `onOff: false`, or it carries no `onOff` key and the
endpoint's `OnOff` attribute already reads `false`. When that write is
stripped, the endpoint's `currentLevel` simply keeps whatever value it
already held — the last level Indigo confirmed while the light was on. A
pushed `level: 0` while the device is on is unaffected and still writes 1,
the owner's confirmed pin that level 0 always means off in Indigo. This is
bridge-node-only: no plugin change, no protocol frame change, `executeIfOff`
stays `true` — the plain-`moveToLevel`-from-off command is still forwarded
exactly as before, it just no longer has a Lighting minimum sitting there to
restore away from.

This is squarely inside the #143/#201 doctrine, not an exception to it: the
value retained in `currentLevel` while off is not invented. It is the last
level Indigo itself confirmed, while the light was genuinely on — the same
confirmed-truth standard every other attribute in this file is held to. What
changes is only *which* confirmed value `currentLevel` represents while the
light is off: previously the (also confirmed, by construction) Lighting
minimum; now the last confirmed on-level. `onOff: false` still reports
truthfully and unconditionally the instant Indigo confirms it — this ADR
does not touch that attribute, or when it moves.

Applies to the three level-bearing roles: `dimmableLight`,
`colorTemperatureLight`, `extendedColorLight`.

### Consequences

* Good, because the measured #353 failure — an Alexa turn-on from off landing at
  100% — stops: there is nothing for the plain `moveToLevel(254)` to act on
  once `currentLevel` already reads the on-level Alexa's `onOff.on` is about
  to restore anyway.
* Good, because it holds the confirmed-truth doctrine exactly as tightly as
  before for every other key and command — this touches one attribute, one
  narrow condition (`level: 0` while off), nothing else.
* Bad, because a brand-new endpoint has no confirmed prior level to retain:
  its first-ever construction push still writes the Lighting minimum
  (`LEVEL_CONTROL_INITIAL`'s `currentLevel: 1`), so #353 persists for such a
  device until its first turn-on. An EXISTING accessory does not share this
  gap across a bridge *restart* — matter.js persists `currentLevel` as a
  nonvolatile attribute and restores it over whatever the construction patch
  seeds, pinned by a live-shaped test.
* Bad, because matter.js's scene recall compares the incoming level against
  the endpoint's current `currentLevel` and skips the level half of the
  recall when they already match. A Matter scene recall to exactly the
  retained level, while the light is off, therefore emits no level command —
  a pre-existing matter.js quirk that was always true, newly reachable now
  that "off" and "the retained level" can coexist on the same attribute.
* Bad, because Alexa's own threshold for "minimum" versus "a normal level"
  is not known to us — only those two extremes have ever been tested, and
  only by upstream, not by this bridge.
* Bad, because while the light is off, a relative Matter command (step/move)
  now computes from the retained level rather than from the minimum — a
  behaviour change for that narrow case, in the same direction as the fix
  itself (it is no longer stepping from a value nobody chose).
* Neutral, because this bends `BRIDGE_PROTOCOL.md` §4.2's earlier "0 ↔ off
  preserved exactly" statement for the outbound direction — amended in the
  same change as this ADR to describe the asymmetry precisely (inbound is
  unaffected; outbound no longer moves `currentLevel` to the minimum while
  off).

### Confirmation

**Confirmed live, 2026-09-19, on the maintainer's system** — a patched bridge
node (published 0.17.3 plus this change and nothing else; the two differed
only in comments) running against a real Alexa fabric, with both arms of the
test captured from the SAME Alexa controller node, on the same build, minutes
apart. The evidence is the bridge node's own log of inbound invocations:

* **Control arm — minimum advertised.** A Zigbee dimmer that had been off
  since before the patched node started, so still advertising the Lighting
  minimum (the fresh/no-history known limit below). A voice "turn on" produced
  `onOff.on` followed 85 ms later by a plain `moveToLevel` `level: 254`, and
  the plugin dispatched `setLevel 100`. This is #353 reproduced, and it shows
  this controller node does send the second command when the level reads
  minimum.
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
lowest retained level tried was 20%), or why Alexa does it — that remains
upstream's inference (`home-assistant-matter-hub#880`), now consistent with
two independent A/Bs.

In the suite, `retainLevelWhileOff` is covered by
`bridge-node/test/registry.test.ts` — retention in a single frame, a bare
`level: 0` push, replay, all three light roles, step-from-retained-level, the
`create`-with-persisted-history path, and the fresh-endpoint known limit —
and by `bridge-node/test/restore.test.ts` for a real node restart
(`restoreEndpoints` → attach → `update` → `applyStates`).

## Pros and Cons of the Options

### A — retain the last confirmed on-level

* Good, because it is entirely bridge-node-side: no plugin change, no
  protocol frame change, `executeIfOff` untouched.
* Good, because it stays inside the confirmed-truth doctrine — the retained
  value is Indigo-confirmed, just applied to a new moment (while off) rather
  than invented.
* Neutral, because it does not explain WHY Alexa sends the second command —
  it removes the thing that command has to act on, which is enough to fix
  the symptom without needing the why.
* Bad, because of the fresh-endpoint and scene-recall known limits above.

### B — seed `executeIfOff: false`

* Good, because it is the smallest possible change — one seeded option flips.
* Bad, because it also drops "Alexa, set X to 70%" sent to an off light,
  which sends the identical On + plain-`moveToLevel` shape — fixed upstream
  only by a further change (HAMH #790/#603, resolved by their PR #855), not
  by this option alone. Rejected as incomplete on its own.

### C — a timing heuristic

* Good, because it is Alexa-fabric-scoped and does not touch `currentLevel`
  semantics at all.
* Bad, because it swallows a genuine "turn on at 100%" request that happens
  to arrive inside the window — the `RiDDiX/home-assistant-matter-hub` fork
  that uses this shape broke Siri's identical "set to 100%" pair when a
  light was already on (their issues #306, #460) and had to make the
  heuristic opt-in and fabric-scoped to ship it at all.
* Bad, because it is the exact timing-heuristic shape this repo's ADR-0013
  already rejected as an option (there, for colour temperature) in favour of
  a push+tolerance mechanism — reintroducing it here for level would be
  inconsistent with that precedent.
* Bad, because it gates a command on believed state, against issue #235's
  standing rule in this file: commands are forwarded, never gated on what
  the bridge believes — Indigo is the one system here that gets to decide
  what a command means.

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
See also `docs/DEVICE-NOTES.md`'s "Alexa's Matter turn-on for dimmable
lights" entry for the user-facing, sanitised write-up.

## For AI agents
- DO: keep `retainLevelWhileOff` scoped to exactly `level: 0` while off — do
  not extend it to other keys or other level values without a similarly
  measured failure.
- DO: keep `executeIfOff: true` and the plain-`moveToLevel`-while-off forward
  path untouched — the fix is what `currentLevel` reads, never whether the
  command reaches the override.
- DO: update this ADR's Confirmation section once a live Alexa A/B has run,
  and move `status` to `accepted` only then — not before.
- DON'T: read the "Decision Outcome" here as license to push an unconfirmed
  value into `currentLevel` for any other case — the value retained here is
  always one Indigo itself already confirmed while the light was on.
- DON'T: re-propose option B, C, or D without new evidence — each was
  rejected against a specific, cited failure mode above, not on convenience.
