---
parent: Decisions
nav_order: 13
title: "ADR-0013: A confirmed, commanded colour-temperature is pushed as state"

status: "accepted"
date: 2026-08-23
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0013: A confirmed, commanded colour-temperature is pushed as state

## Context and Problem Statement

Since #143/#201 the fabric attribute a paired ecosystem sees is Indigo-confirmed
truth only: it moves when `deviceUpdated` says the real device moved, never
because a command was merely accepted. That doctrine loops for colour
temperature when a device permanently clamps. Live on issue #281: Apple
adaptive lighting writes 426 mireds (≈2347K); the plugin clamps and converts to
Indigo's Kelvin exactly as commanded; the z2m-bridged lamp's own hardware warm
limit clamps that to 2500K; the lamp echoes back 400 mireds; `deviceUpdated`
faithfully pushes 400; Apple's own tile still says 426; Apple re-asserts the
same command roughly every 3 seconds, forever, across every warm-limited lamp
in the house. No dispatch is failing — every write reaches Indigo and the lamp
correctly does the warmest thing it can — so `_correct` (F5's "push truth after
a failed command") never fires; the loop runs entirely on *successful*
commands.

## Decision Drivers

* The doctrine is right for every other role and command: an ecosystem must
  never be told a state Indigo has not confirmed. Fixing #281 must not weaken
  that for `onOff`, `level`, `hue`, setpoints, or any command that is not this
  one clamp.
* The symptom is real load, not just a log annoyance: seven lights re-asserting
  every ~3s is continuous command traffic into Indigo for the lifetime of the
  session, and it defeated Auto Lights three times in one evening.
* Whatever closes the loop must be self-healing without new state to manage —
  an `attach`/reconnect already reseeds the snapshot from device truth, and
  that path must keep working unmodified.

## Considered Options

* **A — do nothing.** Rejected: measured to defeat Auto Lights three times in
  one evening; not a tolerable steady state.
* **B — declare the device's physical mireds bounds to the fabric,** so the
  ecosystem itself never asks for less than the lamp can do. Correct
  long-term and entirely node-side (the node would need to read and expose a
  per-accessory `ColorTempPhysicalMinMireds`/`MaxMireds`, which Indigo has no
  source for today — no plugin reports a bulb's warm limit). Out of scope
  here; tracked as the real fix for a future pass.
* **C — track commanded writes with a timer,** the same shape HAMH's own
  adaptive-lighting workaround uses: hold the commanded value for some window
  and suppress a contradicting echo only while it runs. Rejected: more state
  (a per-device timer, a window to tune) for a narrower win than option D, and
  a wrong window either reopens the gap early or suppresses a genuine change
  late.
* **D — push the commanded value as state, once, right after a successful
  dispatch; pair it with a tolerance on the echo.** Chosen.

## Decision Outcome

Chosen option: **D**. `ExportHandler.commanded_states(command, args)` lets a
role say which §4.2 states a *successful* command entitles the caller to
consider pushed. It answers `{}` for every command by default — the #143/#201
doctrine stands everywhere it is not explicitly overridden.
`ColorTemperatureLightExport` overrides it for `setColorTemp` only, returning
the identical plugin-clamped mireds `_set_color_temp` itself sent to Indigo —
never a different value, never a different key.

`ExportBridge._apply_command`'s success branch folds a non-empty
`commanded_states` result into the pushed snapshot and pushes it, through a
new `_push_commanded` that mirrors `_correct` exactly: gated on a live client,
folds through `_note_pushed` (so a mode-alternating sibling — hue/saturation —
is evicted rather than left stale, #282's trap (b)). If the client is not
live, nothing is pushed and nothing is noted; the next `attach` reseeds from
device truth as it always has, and the next real ecosystem write reconverges
on its own — no new retry logic, no new state to leak.

`CT_TOLERANCE_MIREDS` (50) is the second half: without it, the lamp's next
clamped echo (400) would immediately overwrite the commanded push (426) and
reopen the gap on the very next `deviceUpdated`. Unlike hue — which carries a
tolerance because the Matter 0–254 round trip is genuinely lossy — an in-range
mireds→Kelvin→mireds round trip is exact, so this tolerance is not absorbing
conversion noise. It is headroom for a permanent, one-sided clamp gap: the
measured #281 gap is 26 mireds, 50 covers it with margin, and 50 stays far
below any deliberate colour-temperature change a user or ecosystem would make
(warm↔cool spans 200+ mireds).

### Consequences

* Good: the loop measured on #281 stops. Once a successful `setColorTemp`
  lands, the fabric attribute reads what was actually commanded, and the
  lamp's own subsequent clamped echo reads as unchanged rather than
  contradicting it — which is what lets the real Apple stop re-asserting.
* Good: the doctrine is unweakened everywhere else. `commanded_states`
  defaults to `{}`, so `onOff`, `level`, setpoints, lock/unlock and every
  other command still wait for `deviceUpdated` to confirm before the
  ecosystem is told anything.
* Good: self-healing, not a new failure mode. A push that cannot go out
  (client not live) is simply not noted either, exactly like every other push
  in this file — the next attach or the next real device report puts it
  right, with no timer to leak or expire wrong.
* Bad: a genuine Indigo-originated colour-temperature drift of up to 50
  mireds from the last push is not reported until it accumulates past the
  tolerance — the same trade-off `HUE_TOLERANCE_DEGREES` already accepted for
  hue, applied here at a coarser grain because the gap being absorbed is
  coarser.
* Bad: `commanded_states` is a second place (besides `_set_color_temp` itself)
  that has to apply the identical mireds clamp. They are kept in lockstep by
  both reading the same `MIREDS_MIN`/`MIREDS_MAX` and the same `round`/`_clamp`
  expression; a zoo-style test pins the pair.
* Neutral: `_push_commanded` fires on every successful `setColorTemp`
  dispatch, not only the first — a repeated identical command produces a
  repeated identical push. Cheap and idempotent at the node (§3.4), the same
  posture every other fire-and-forget push in this file already takes.

### Confirmation

`tests/test_export_handlers.py::TestColorTemperature` (the tolerance and
`commanded_states` behaviour) and `tests/test_export_bridge.py::
TestCommandedStatePush` (the end-to-end push, the live-client gate, the #282
eviction interaction, and a simulated multi-round #281 loop that must reach
the wire as a single settled value).

## More Information

Issue #281. Builds on the doctrine established by #143/#201 (node reports only
Indigo-confirmed truth) and the mode-alternating eviction mechanism from issue
#282 (`ExportHandler.mode_alternating_keys`, `ExportBridge._note_pushed`).
`HUE_TOLERANCE_DEGREES` is the precedent for a diff tolerance existing at all
in this module — `CT_TOLERANCE_MIREDS` is a different *kind* of tolerance
(clamp-gap headroom, not conversion-lossiness) applied through the same
mechanism.

## For AI agents
- DO: keep `commanded_states` empty (`{}`) for every command and role unless a
  *measured, permanent* device-side clamp is looping — this is a narrow,
  scoped exception to the #143/#201 doctrine, not a template for optimistic
  state in general.
- DO: keep `commanded_states`'s clamp expression byte-for-byte identical to
  the dispatch handler's own clamp — a divergence here pushes a value Indigo
  was never actually asked to set.
- DON'T: add a tolerance to a new state key without checking whether it is
  absorbing real conversion lossiness (like hue) or a device-specific clamp
  gap (like colour temperature) — the justification and the size both follow
  from which one it is.
- DON'T: reach for a timer-based "hold the commanded value" mechanism for a
  future case like this one; prefer the push+tolerance shape unless a genuine
  need for a bounded time window is measured, not assumed.
