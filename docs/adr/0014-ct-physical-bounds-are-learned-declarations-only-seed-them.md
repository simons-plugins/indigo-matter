---
parent: Decisions
nav_order: 14
title: "ADR-0014: Colour-temperature physical bounds are learned from clamped echoes; declarations only seed them"

status: "accepted"
date: 2026-08-24
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0014: Colour-temperature physical bounds are learned from clamped echoes; declarations only seed them

## Context and Problem Statement

ADR-0013 converges the #281 adaptive-lighting conversation pragmatically, and
its own text names the boundary: a device whose clamp gap exceeds
`CT_TOLERANCE_MIREDS` (30) needs honest physical bounds published to the
fabric, not a wider band. That boundary was crossed the same evening ADR-0013
shipped — Apple's adaptive target warmed to 444+ mireds overnight while the
measured warm limits sit at 454 (Hue) and 400-class values elsewhere, so the
gap outgrew 30 and nine lamps resumed re-asserting (harmless now, but a
standing storm). The principled fix is what native Matter bulbs do: declare
`ColorTempPhysicalMinMireds`/`MaxMireds`, so a controller scales its request
into what the lamp can actually reach and the write and the echo agree by
construction.

The question this ADR decides is **where the bound's truth comes from**. Every
available declaration is only a claim: the z2m plugin's per-device props
claimed 2200K for a strip whose silicon clamps at 2500K; z2m's own converter
metadata declares 153–500 for lamps measured at 200–454; even a user typing a
datasheet value can be wrong. Native Matter bulbs get away with declared
bounds because claim and hardware share a vendor — ours never do.

## Decision Drivers

* A wrong published bound is worse than none: it either re-opens the #281 loop
  (bound too wide) or forbids colours the lamp can genuinely render (bound too
  narrow), and both wear the authority of a fabric attribute.
* The ADR-0013 machinery already surfaces ground truth for free: a commanded
  426 answered by a confirmed echo of 400 *is* the device stating its warm
  floor — no probe traffic, no new wire protocol.
* Bounds must correct themselves in both directions with no manual step; the
  MQTT probe that produced the measured table is not something a user can be
  asked to repeat.
* An update after first pairing must actually reach controllers that cache
  capabilities at commissioning time.

## Considered Options

* **A — user/plugin-declared bounds only** (the issue's original sketch).
  Rejected as the mechanism: a declaration is a claim, and the one measured
  data point we had (the MiBoxer props) was a lie. Kept as the *seed*.
* **B — learned bounds from observed clamping, seeded by declarations.**
  Chosen. The commanded-vs-echo gap that ADR-0013 tolerates is treated as
  evidence: N consistent same-side shortfalls adopt the echoed value as the
  learned bound; any confirmed reading *outside* the current bounds proves
  reach and re-widens immediately. Declared/user values only matter before the
  first evidence arrives.
* **C — active probing** (write both extremes, read the echoes — the method
  used to build the measured table). Rejected as **automatic** plugin
  behaviour: it visibly flashes real lamps, needs an idle window, and the
  passive evidence in B arrives anyway within one adaptive-lighting cycle.
  Revisited post-acceptance: passive evidence has a real gap B does not
  close — shortfall adoption requires a *commanded* reference
  (``ct_learner.CTBoundsLearner.record_commanded``), and that only ever
  comes from a §5 command the FABRIC sent. A lamp Indigo drives by scene,
  schedule, or hand — never by a paired ecosystem — has no commanded
  reference to learn a shortfall against, ever, however many times its
  colour temperature changes; only the re-widen path (a reading past the
  CURRENT bounds) can move for it, which cannot narrow a seed that started
  too wide. C is therefore shipped after all, but not as the rejected
  automatic mechanism: as an **explicit, operator-invoked action**
  ("Calibrate Colour-Temperature Bounds", ``ct_calibration.py``) that a user
  runs deliberately, on their own schedule, against a lamp they have chosen
  to accept the visible flash on (an OFF lamp does not flash at all — the
  action defaults to skipping lit ones). The measurements it takes feed the
  SAME learner and the SAME store B already writes to
  (``CTBoundsLearner.adopt_measured`` funnels into the existing ``_adopt``)
  — there is still only one bounds mechanism, B; C is now a second, consented
  way to hand it evidence, not a second mechanism. Passive evidence remains
  the core, unattended path for every device the action is never run
  against.

## Decision Outcome

Chosen option: **B**, implemented on both sides of the bridge protocol
(issue #293):

* **Wire**: `EndpointSpec.options` gains `ctMinMireds`/`ctMaxMireds` for the
  two CT-bearing roles. The pair is usable iff both are integers and
  `153 <= min < max <= 500`; anything else is treated as absent with one
  warning — never a refusal, because a refused attach takes every export
  offline over one bad option. The plugin sends only the **effective** pair,
  and only when it differs from the generic 153/500, so an ordinary export's
  wire frame is byte-identical to before.
* **Node**: usable bounds are applied at construction (physical min/max,
  `coupleColorTempToLevelMinMireds`, and the startup `colorTemperatureMireds`
  clamped into them) and on live update/rekey via an ordinary attribute write,
  with a `ConfigurationVersion` bump so capability-caching ecosystems re-read.
  `set_state` colour-temperature writes clamp to the endpoint's *live* bounds.
* **Plugin storage**: four per-export option keys — `ctMinMireds`/`ctMaxMireds`
  (the user seed, edited as Kelvin in the export dialog) and
  `ctLearnedMinMireds`/`ctLearnedMaxMireds` (evidence). Effective bound per
  side = learned, else seed, else generic. Learned keys never leave the plugin.
* **Learning**: `_push_commanded` records the commanded mireds; `device_updated`
  feeds every confirmed reading to the learner *before* diff-tolerance
  suppression. Two consecutive same-side shortfalls with a consistent echoed
  value (±1 mired), each within a 15-second freshness window of a
  `setColorTemp` dispatch, adopt the echo as that side's learned bound —
  persist, republish (`upsert_endpoint`), one INFO line. Any reading outside
  the current effective bounds re-widens immediately, window-free — which is
  also what self-heals a wrong user seed. ADR-0013's commanded push itself is
  untouched: it still pushes exactly what was commanded, clamped only to the
  generic range, because clamping it to effective bounds would re-open the
  very disagreement it exists to close during the learning window.

### Consequences

* Good: for the true hardware clampers (Hue 454, Innr 200–454) the controller
  stops asking for the unreachable value at all — the #281 broker-tap
  signature disappears without ADR-0013's tolerance having to absorb anything.
* Good: no bound has to be right on day one. A wrong seed is corrected by the
  first evidence in either direction, and a device with no seed converges
  within two adaptive ticks of its first clamped command.
* Good: the ADR-0013 machinery is kept unweakened as defence-in-depth for
  devices that clamp *inconsistently* and during the learning window.
* Bad: the freshness window is exactly the timer-shaped state ADR-0013's
  "DON'T" cautioned against. It is accepted here because the need is measured,
  not assumed: without it, an Indigo-side scene change hours after the last
  ecosystem command reads as a shortfall and adopts a wrong bound — the
  degradation-path tests pin that trap.
* Bad: adoption writes plugin prefs and republishes from a device-update code
  path that never wrote before; the write is rare (a bound changes at most a
  handful of times per device, ever) but it is a new store writer to reason
  about.
* Neutral: whether a *paired Apple* re-computes its adaptive curve from a
  live bounds report + `ConfigurationVersion` bump — rather than only at
  commissioning — is a live-test question; the documented fallback is
  remove/re-add of the accessory, and bounds set before first pairing need
  nothing.

### Confirmation

Plugin: learner adoption/refusal/re-widen suites in `tests/` (the stale-window
trap, single-observation refusal, inconsistent-echo refusal, min>=max refusal,
seed-correction re-widen, wire-transform byte-identity). Node: construction,
live-update, `ConfigurationVersion` and clamp tests in `bridge-node`. Shared:
ct-bounds golden frames in `tests/fixtures/bridge_protocol/frames.json`, which
both suites consume by design. Live: the #281 broker-tap signature on jarvis
before/after (issue #293's acceptance).

## More Information

Issue #293 (design + Simon's learned-bounds revision + the measured hardware
table), issue #281 (mechanism), ADR-0013 (the stabilizer this builds on and
keeps). The measured ranges that seed the first deployment: Hue pendants
153–454, Innr sideboards 200–454.

## For AI agents
- DO: treat declared or user-entered CT ranges as *seeds* — never as authority
  that overrides a learned bound, in either direction.
- DO: keep the learner's evidence gate intact: shortfall evidence counts only
  inside the freshness window of a real `setColorTemp` dispatch; widening
  evidence counts always.
- DON'T: clamp ADR-0013's commanded push to the effective bounds — the push
  must keep converging to what the ecosystem wrote, or the loop re-opens for
  any controller that has not yet re-read the bounds.
- DON'T: turn an unusable options pair into an attach refusal on the node —
  one bad option must never take every export offline.
