# How indigo-matter is tested

This plugin sits between two systems that are both awkward to test — Indigo's
plugin host and a live Matter fabric — so it is tested in four layers, each
catching what the previous one can't. This page explains the layers, what each
has actually caught, and how to reproduce or extend them.

| Layer | What it is | What it catches |
|---|---|---|
| 1. Unit suite | 2,417 pytest tests, everything mocked | Logic, protocol parsing, reconcile/state machinery |
| 2. Device zoo | Contract invariants over cluster combinations | Whole *classes* of device-mapping bugs |
| 3. Virtual fleet | 15+ real Matter devices (matter.js) on the LAN | Real commissioning, subscriptions, command round-trips |
| 4. Real hardware | Shipping devices on the production path | Everything else — including what Indigo itself really does |

---

## 1. Unit suite

```bash
cd indigo-matter && python3 -m pytest -q
```

No Indigo server and no matter-server needed: the `indigo` module is mocked
(`tests/conftest.py`) and matter-server is faked at the WebSocket frame layer
(`tests/fakes.py`), so the real client/reconcile/dispatch code paths run
against recorded protocol shapes. `tests/test_golden_real.py` pins parsing
against captured frames from a live matter-server, so a wire-format drift
fails loudly.

A deliberate piece of test design worth knowing: the fake Indigo device models
the **pessimistic** behaviours we have observed or must guard against — e.g.
it derives the list-display state from `Supports*` props at creation and does
*not* re-derive it on a props replace — so the self-heal and warning paths are
exercised under worst-case Indigo behaviour even though real Indigo turned out
to be kinder (see layer 4).

## 2. The device zoo (`tests/test_device_zoo.py`)

Matter devices arrive with cluster combinations nobody predicted — the spec
*requires* some surprises (a Matter 1.2+ fan must expose OnOff alongside
FanControl; an Extended Color Light need only implement XY + ColorTemperature,
HueSaturation is optional). The zoo is a contract harness for exactly this:
a table of synthetic nodes (`ZOO`) run through the **real** handler registry,
with structural invariants asserted over every entry:

1. each endpoint maps to exactly the expected Indigo device types;
2. **at most one actuator device per endpoint** — two would fight over one
   physical device;
3. every spec's `device_type_id` exists in `Devices.xml`;
4. every seeded initial state is XML-declared or an Indigo built-in;
5. every sensor-type spec carries explicit display props
   (`SupportsOnState`/`SupportsSensorValue`), because Indigo derives the
   device-list display from creation props, never from Devices.xml statics
   (issue #56) — with `(False, False)` allowed only when the XML declares a
   `UiDisplayStateId` fallback pointing at a declared custom state.

**Track record:** on its very first run the zoo caught a colour light without
LevelControl producing a duplicate relay (nobody had predicted that case), and
during the issue #56 follow-up it caught the button handler declaring display
props without merging them into its creation spec.

**When a strange device shows up in the wild:** add its cluster set as one
`ZOO` entry (cluster ids in decimal inside the `"ep/cluster/attr"` keys) with
the device types you expect, and every invariant runs over it automatically.
That is the intended first response to any "weird device" bug report.

## 3. The virtual device fleet (matter.js)

Real Matter devices, minus the shopping: the
[`@matter/examples`](https://github.com/project-chip/matter.js) package plus
small custom scripts compose genuine commissionable Matter nodes on the LAN —
the plugin and matter-server cannot tell them from shipping hardware. The
development fleet runs 15+ devices covering relay, dimmer, extended colour
light, temperature/humidity, thermostat, fan, window covering, door lock,
valve, generic switch (button), smoke/CO, air quality (AQ+CO₂+PM2.5+TVOC),
pressure/flow, energy plug, and battery sensor.

Scripts live in `/tmp/matter-test/*.mjs` on the dev machine (commissioned
identities persist in `~/.matter/<id>`, so relaunching keeps the node).
Check and relaunch with:

```bash
pgrep -fl "node .*\.mjs"                 # what's running
cd /tmp/matter-test && node fan.mjs >> fan.log 2>&1 &
```

Hard-won rig rules (each cost us a debugging session):

- **One UDP port per device** (`network.port`); two devices on one port sends
  PASE to the wrong socket.
- **Distinct discriminators with different top nibbles** — the 11-digit manual
  code only carries the 4-bit short discriminator.
- **Commission within ~15 minutes** of first launch; then the device stops
  advertising.
- After cycling many device instances, **restart matter-server** to clear its
  mDNS cache.
- Custom devices must enable their cluster **features** explicitly
  (e.g. `ThermostatServer.with("Heating", "Cooling")`) and must not set
  server-managed attributes.

The fleet is also deliberately imperfect in useful ways: the colour light is
an `ExtendedColorLightDevice` with default features — XY only, no
HueSaturation — which is exactly how it exposed issue #60 (RGB commands were
sent as `MoveToHueAndSaturation` and rejected with `UNSUPPORTED_COMMAND`).
Keep test devices spec-minimal rather than maximal: minimal devices find more
bugs.

## 4. Live validation on a production Indigo server

Some truths only real Indigo knows — its API behaviours are not all
documented, and several were established empirically here:

- `Supports*` capabilities and `UiDisplayStateId` semantics for API-created
  devices: creation props rule; a True `Supports*` wins; with both explicitly
  False, the XML `UiDisplayStateId` applies after all (issues #56/#58/#59).
- `replacePluginPropsOnServer` **does** re-derive the display state — proven
  by deploying a fix over a fleet of pre-fix devices and watching two
  reconciles (heal pass, then a clean pass with zero stale-display warnings).

The method, reproducible by any plugin dev with a test server:

1. rsync the build into the live plugin bundle (updates only — first installs
   must be double-click installed), then restart the plugin;
2. read the event log — the plugin is written so the log *is* the evidence:
   self-heals log what they changed and verify persistence by reading props
   back (an unverified write is logged as a warning, never as success);
3. restart again — the steady state must be quiet; anything still healing or
   warning on pass two is a finding;
4. for display/UI questions, temporary instrumentation (an INFO line dumping
   `displayStateId` per device) on the deployed copy answers in one restart —
   remove it before committing anything.

**Real hardware validations on the production path:**

| Device | Transport | Date | Result |
|---|---|---|---|
| TP-Link Tapo P110M | Wi-Fi | 2026-06-10 | Full Domio share-model flow; on/off + live energy after Tapo's Matter 1.3 firmware update |
| Aqara FP300 | Thread (HomePod TBR) | 2026-06-12 | Share model first try, ~10 s join, 4 endpoints, battery fan-out, unprompted live reports (third-party tester, matter-server 0.6.8) |

The FP300 test also delivered the bug report that became issue #56 — external
testers running real devices are part of the methodology, not an afterthought.
If you test a new device, file what you find (good or bad) at
[github.com/simons-plugins/indigo-matter/issues](https://github.com/simons-plugins/indigo-matter/issues),
ideally with the endpoint's cluster list from
`GET …/message/com.simons-plugins.indigo-matter/diagnostics?nodeId=0x…` — unknown
devices also appear in Indigo as a *"Matter Device (unsupported clusters)"*
placeholder whose settings list exactly the cluster ids to report.
