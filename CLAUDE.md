# CLAUDE.md — indigo-matter

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: Matter (indigo-matter)
- **Type**: Indigo plugin
- **Shortcut**: `matter`
- **GitHub**: https://github.com/simons-plugins/indigo-matter
- **Language**: Python 3.11+ (`CFBundleIdentifier` `com.simons-plugins.indigo-matter`)

## Role in the workspace

Brings Matter device support to Indigo. Manages a `matter-server` instance (Node,
python-matter-server-compatible WebSocket protocol), holds the Indigo fabric, and
translates Matter clusters ↔ Indigo device types so Matter devices behave as
first-class Indigo devices. **Domio owns commissioning UX** (one-shot per device);
this plugin owns runtime control for the device's lifetime.

```
Domio (iOS, MatterSupport) ──commission+handoff──▶ indigo-matter ──WebSocket──▶ matter-server ──▶ Apple TV (TBR) ──▶ Thread/Wi-Fi devices
        │                                                  ▲
        └──────────── HTTP API (API.md, via IWS) ──────────┘
```

## Related projects

- [`../domio-code/`](../domio-code/) — iOS app; owns commissioning. The Domio↔plugin HTTP contract is **`docs/API.md`** (mirrored in both repos; keep in sync).
- The Matter architecture decision (the **share model**: the user's existing ecosystem commissions as admin 1, the plugin joins as a second admin over IP, the ecosystem's hub is the Thread border router) is explained for users in [`docs/MATTER.md`](./docs/MATTER.md).

## Architecture (this plugin)

Two async subsystems run on a **single asyncio event loop** on a dedicated
background thread (`async_runtime.py`); Indigo's `runConcurrentThread` is a non-I/O
watchdog. Indigo→loop bridges via `runtime.submit(coro).result(timeout)`;
loop→Indigo writes go straight through `device_sync.apply_states` (thread-safe IPC).

Two directions, sharing almost nothing:

- **Inbound** — Matter devices Indigo controls. `matter_client.py` speaks to a
  `matter-server` Node process; `device_sync.py` reconciles nodes into Indigo
  devices; `matter_handlers/` holds one handler per cluster.
- **Outbound** — Indigo devices exported as Matter accessories. `export_bridge.py`
  drives a plugin-owned matter.js node in `bridge-node/`, the **only** place
  matter.js is imported (workspace ADR-0006).

`plugin.py` is lifecycle glue and the device/action bridge; everything Indigo
reaches by XML-named callback lives in one of seven mixins, because Indigo resolves
those names as attributes on the `Plugin` class (issue #146).

**→ Per-module detail is in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).**
Read it before changing a module — most rows record a decision reached the hard
way and say why the obvious alternative was rejected.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points:

- **Version bump per PR**: `Info.plist` `PluginVersion` (format `YYYY.R.P`; the plist is the source of truth for the current value).
- **Testing**: `pytest` (`pyproject.toml`, pylint + 120-char like netro). matter-server is mocked at the WS layer (`tests/fakes.py`); the `indigo` module is mocked. Run: `cd indigo-matter && pytest`. The bridge node has its own suite: `cd bridge-node && npm run build && npm test`.
- **Bridge-protocol golden frames**: `tests/fixtures/bridge_protocol/frames.json` is the ONE shared fixture file (BRIDGE_PROTOCOL §7) — the Python suite reads it directly, `npm test` copies it into the TS build. Change a frame and both suites must be updated, by design.
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead.
- **Architecture decisions**: repo-local ones live in [`docs/adr/`](./docs/adr/INDEX.md) (MADR 4.0.0). **Read that index before changing writable settings, capability detection, or the diagnostics surface.** Cross-repo concerns go in the workspace `docs/adr/` instead. Numbering is independent and collides, so always qualify a reference as "workspace ADR-NNNN". ADRs are immutable once accepted — supersede, never edit.
- **Device quirks**: hardware behaviours that surprised us are in [`docs/DEVICE-NOTES.md`](./docs/DEVICE-NOTES.md).

## Releasing the bridge node (read this BEFORE `npm publish`)

`bridge-node` ships as an **exact-pinned npm package**, not as part of the
plugin bundle. A change to `bridge-node/src/` reaches nobody until *both* of
these happen — merging it does nothing on its own:

1. `npm publish` the new `bridge-node/package.json` `version`, and
2. move `DEFAULT_INSTALL_SPEC` in `Contents/Server Plugin/bridge_agent.py` to
   that version. That is "the one line a release bumps", as its own docstring
   says.

Order matters: **publish first**. If the pin reaches `main` before the version
is on npm, every install resolving it fails.

### The npm token lives in `.env`, and must be passed explicitly

The publish credential is `npmjs-token` in this repo's **`.env`** (gitignored,
untracked — keep it that way). There is deliberately no ambient npm credential:
`~/.npmrc` holds no token, so a bare `npm publish` fails outright rather than
half-working under the wrong identity.

```sh
cd bridge-node
TOKEN=$(grep -i 'npmjs-token' ../.env | cut -d= -f2- | xargs)
npm publish "--//registry.npmjs.org/:_authToken=$TOKEN"
```

**Why this is written down.** `npm login` writes a token of its own into
`~/.npmrc`, and npm prefers it silently. When that happened here, a publish with
the correct `.env` token in hand still failed — and npm reports an
unauthorised write as `404 Not Found - PUT`, never `403`, so it reads as "no
such package" rather than "wrong credential". That combination cost a long
debugging detour. If you see that 404, suspect the credential, not the package,
and check for a token in `~/.npmrc` first.

Do not run `npm login`, and do not store the token anywhere but `.env`.

### Verifying and shipping

`npm view` serves cached metadata and will lie to you immediately after a
publish. Check the registry directly:

```sh
curl -sS https://registry.npmjs.org/indigo-matter-bridge \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['dist-tags']['latest'])"
```

Then open the pin-bump PR with **`[release]` in the title** (releases are
opt-in). Note `version-check` runs on *every* PR to `main` with no path filter,
so even a docs-only PR needs a `PluginVersion` bump.

## Status

Both directions are built, released, and field-validated against real hardware:
inbound control over Wi-Fi (Tapo P110M) and Thread (Aqara FP300 via a HomePod
border router), and outbound export of Indigo devices as Matter accessories
through the bridge node. The current version is in `Info.plist`, which is the
source of truth.

Work is issues-driven — read the GitHub issue list rather than a milestone plan.
`docs/PRD-indigo-matter-plugin.md` and `docs/PRD-indigo-matter-export.md` record
the original scope of each direction; both are historical.

Before writing plugin code, invoke `/indigo:dev`. Always `date -u` for timestamps.
