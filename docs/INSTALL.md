# Installing matter-server for indigo-matter

This guide walks through installing the **matter-server** runtime that the
`indigo-matter` plugin drives, and configuring the plugin to connect to it. It is
derived from the live managed-LaunchAgent bring-up on the reference Indigo server.

If you just installed the plugin and Indigo is logging
`Connect call failed ('127.0.0.1', 5580)`, you are in the right place — the plugin is
running but matter-server is not.

---

## Overview

The `indigo-matter` plugin does not speak Matter directly. It manages a separate
Node.js process — **matter-server** (npm package
[`matter-server`](https://github.com/matter-js/matterjs-server), currently **Beta
1.2.2**) — which holds the Indigo-owned Matter fabric and exposes a WebSocket control
API. The plugin connects to that WebSocket and translates Matter clusters into
first-class Indigo devices.

A few things to know before you start:

- **Wi-Fi and Thread devices both work.** What this plugin can't do is *first-admin*
  commissioning — handing a factory-fresh device its network credentials over BLE, which
  matter-server can't do because BLE isn't enabled on macOS. That step belongs to a device
  that has a BLE radio: a phone, or an ecosystem hub. Everything after it is plain IP, so
  Thread devices join and are controlled exactly like Wi-Fi ones, via the border router.
- **Two ways to add a device, and they're equivalent.** The **Domio iOS app**, or the
  plugin menu's **Commission device by setup code…** — both take a pairing code, both
  handle Wi-Fi and Thread. Domio is not required.
  - *Share model (the usual path):* an ecosystem you already own — Apple Home, Alexa,
    Google Home — commissions the device first and owns the BLE step; the device is then
    shared to Indigo over IP and the plugin joins as a second admin.
  - *Factory-fresh Thread via Domio:* iOS commissions the device to the iPhone as part of
    that flow, which is what supplies the Thread credentials.
  - *Already on your network, never commissioned:* its printed code works directly, from
    either entry point.

  However it was added, the plugin owns runtime control for the device's lifetime.
- **matter-server is Beta software.** Pin the version, back up the fabric, and expect
  occasional rough edges.

---

## Prerequisites

- **macOS** running your Indigo Server.
- **Indigo 2025.2+** with the `indigo-matter` plugin installed (≥ **2026.0.6** — earlier
  builds launched matter-server via `npx` and no longer work; see Troubleshooting).
- **Node.js ≥ 22.13.0** (required by matter-server 1.2.2), via **Homebrew** or nvm (Step 1).
- Network: the Indigo Server on the same LAN as your Matter devices — Wi-Fi ones
  directly, Thread ones via their border router.

The plugin's only Python dependency (`websockets`) is auto-installed from
`requirements.txt`; if that silently fails, install it manually per the note in that
file.

---

## Step 1 — Install Node.js 22

Pick **one** of the following. **Homebrew is the paved road** — it puts node at a
stable, launchd-visible path (`/opt/homebrew/bin`), so the plugin's auto-detect just
works and you leave `nodeBinDir` blank. nvm works too but is fussier (per-version
paths that change on upgrade, and launchd doesn't source your shell's nvm) — treat it
as the advanced option and pin `nodeBinDir` explicitly.

### Option A — Homebrew (recommended)

```bash
brew install node@22
brew link node@22
node --version    # confirm v22.x
```

Leave **Node bin directory** (`nodeBinDir`) blank — auto-detect finds Homebrew first.

### Option B — nvm (advanced)

```bash
# Install nvm if you don't have it (see https://github.com/nvm-sh/nvm)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
# Restart your shell, then:
nvm install 22
nvm use 22
node --version    # confirm v22.x
```

With nvm, node lives at `~/.nvm/versions/node/<your-version>/bin` (run the command
above to see *your* exact version — don't copy someone else's). The plugin auto-detects
nvm (default alias, else newest installed), but **pin the exact directory** in the plugin
config (`nodeBinDir`) so a node upgrade or a wrong auto-detect can't swap the node out
from under an installed package.

---

## Step 2 — Install matter-server

The package lives at `~/indigo-matter/node_modules/matter-server`, where the plugin's
managed LaunchAgent expects it.

**Easiest (recommended): let the plugin do it.** In local/managed mode, use
**Plugins ▸ Matter ▸ Install/update matter-server**. The plugin installs the package
with the *same* node it will run the server with, and pins that node in `nodeBinDir` —
which is the thing that prevents the most common failure (installing with one node and
running with another, whose native modules won't load). No Terminal needed. Skip to
Step 3.

**Manual (self-managers):** install it yourself with the node you intend to run:

```bash
mkdir -p ~/indigo-matter
npm install --prefix ~/indigo-matter matter-server@1.2.2
```

This creates `~/indigo-matter/package.json` with `"matter-server": "1.2.2"` — an **exact
pin** (no caret). The package is fast-moving Beta, so the plugin pins a specific
validated version rather than floating; the menu install action uses this same version.
Don't install globally — a project-local install keeps the version controlled.
**matter-server 1.2.2 requires Node ≥ 22.13.0** (Step 1). To move to a newer server later,
bump the version deliberately and re-validate.

> **Note — there is no `matter-server` command.** The npm package has no `bin`
> (`"bin": null`); its entry point is `dist/esm/MatterServer.js`. matter-server must be
> launched as
> `node ~/indigo-matter/node_modules/matter-server/dist/esm/MatterServer.js …`. The
> plugin (≥ 2026.0.6) does exactly this in its managed LaunchAgent — you do not need to
> remember it unless you run matter-server manually (Step 3, Mode B).

---

## Step 3 — Configure the plugin

Open **Plugins → Matter → Configure…** in Indigo. There are two ways to run
matter-server.

### Mode A — Plugin-managed LaunchAgent (recommended)

Let the plugin install and supervise matter-server for you.

1. Tick **Manage matter-server LaunchAgent automatically**.
2. **Homebrew users: leave Node bin directory blank** (auto-detect). Only if you used nvm,
   set it to *your* node bin dir — run `echo ~/.nvm/versions/node/$(nvm version)/bin` to get
   the exact path; don't copy a version string from these docs.
3. Leave **matter-server listen address** at `127.0.0.1` (loopback — see Security).
4. Leave **port** `5580`, **WebSocket path** `/ws`, **host** `localhost`, **primary
   interface** `en0` (change only if your active interface differs — check with
   `ifconfig | grep "status: active"`).
5. Save.

The plugin writes `~/Library/LaunchAgents/com.simons-plugins.indigo-matter.plist`
(`RunAtLoad` + `KeepAlive`, running as the Indigo user) and bootstraps it via `launchctl`.

> If a LaunchAgent with the same label is already loaded (e.g. from a previous run or a
> hand-installed plist), the new one won't take over until you boot the old one out:
> ```bash
> launchctl bootout gui/$(id -u)/com.simons-plugins.indigo-matter
> ```
> then restart the Matter plugin (or re-save the config) to re-bootstrap.

> **Don't hand-edit the plist.** The plugin regenerates it from your config on every
> restart; manual edits are overwritten.

### Mode B — Manual

Run matter-server yourself and leave **Manage matter-server LaunchAgent automatically**
**off**. The plugin just connects to whatever is listening on the configured host/port.

A minimal `run.sh`:

```bash
#!/bin/sh
node ~/indigo-matter/node_modules/matter-server/dist/esm/MatterServer.js \
  --storage-path "$HOME/Library/Application Support/com.simons-plugins.indigo-matter/matter-server" \
  --primary-interface en0 \
  --listen-address 127.0.0.1
```

(nvm users: `source ~/.nvm/nvm.sh && nvm use 22` first, or use the absolute node path.)

### Config field reference

| Field | Default | Purpose |
|---|---|---|
| **matter-server host** | `localhost` | Client connect target the plugin dials. |
| **matter-server port** | `5580` | WebSocket port. |
| **matter-server WebSocket path** | `/ws` | WebSocket path. |
| **Node bin directory (optional)** | *(blank)* | Directory containing `node`. Blank = auto-detect (Homebrew, then nvm, then PATH). nvm users should pin their own path (see Step 1). Managed mode only. |
| **matter-server storage path** | `~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server` | The fabric. See Backups. |
| **Manage matter-server LaunchAgent automatically** | off | On = plugin supervises matter-server (Mode A); off = you run it (Mode B). |
| **Primary network interface** | `en0` | macOS interface matter-server binds to. Managed mode only. |
| **matter-server listen address** | `127.0.0.1` | IP the managed server binds its (unauthenticated) control API to. Keep loopback. Managed mode only. See Security. |
| **Allow test/development device certificates** | off | Passes `--enable-test-net-dcl` to the managed matter-server, trusting the Matter test-net DCL alongside the production one (production verification is unchanged). It **also** allows test-net OTA firmware to be offered to commissioned devices. Turn on only if commissioning fails on device attestation because the device uses a test certificate (Homebridge and other dev bridges do). This is a global setting that stays on until you turn it off — untick it once the device is paired. Managed mode only; takes effect on plugin reload or via Plugins ▸ Matter ▸ Restart matter-server. While it is on, the plugin logs a warning on every startup. |
| **Verbose Matter logging** | off | Extra plugin logging. |
| **Trace matter-server WebSocket messages** | off | Logs raw WS frames (debug). |

---

## Step 4 — Verify

After saving (Mode A) or starting matter-server (Mode B), check two places.

**Indigo event log** should show:

```
connected to matter-server, listening
reconciled N Matter node(s)
```

(`N` is 0 on a fresh install — that's fine.)

**matter-server's own log** at
`~/Library/Logs/indigo-matter/matter-server.log` should show:

```
Webserver listening on http://127.0.0.1:5580
```

If matter-server crashed on launch, the reason is in
`~/Library/Logs/indigo-matter/matter-server.err.log`.

Once both are healthy, add a device — from the **Domio app**, or via **Plugins ▸ Matter ▸
Commission device by setup code…** — and it will appear as a native Indigo device.

---

## Backups

> **The single most important thing in this whole setup.**

The Matter fabric lives at:

```
~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server/
```

This directory holds the fabric root CA private key and every device's operational data.
**Losing it means re-commissioning every Matter device from scratch.** Indigo's own
database backup does **not** cover it.

The plugin's in-app backup menu item is currently a stub
([issue #26](https://github.com/simons-plugins/indigo-matter/issues/26)), so back it up
yourself. With the plugin (or matter-server) stopped, copy the directory:

```bash
cp -R \
  "$HOME/Library/Application Support/com.simons-plugins.indigo-matter/matter-server" \
  "$HOME/Backups/indigo-matter-fabric-$(date -u +%Y%m%d)"
```

Keep it somewhere safe and off-machine.

---

## Security

matter-server's WebSocket control API is **unauthenticated**, and with no
`--listen-address` it binds **all** interfaces — meaning anyone on your LAN could control
your entire Matter fabric.

The plugin defaults the managed server to `--listen-address 127.0.0.1` (loopback) via the
**matter-server listen address** config field. **Keep it on loopback** unless
matter-server runs on a different host and you have a trusted firewall in front of it.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `npm error could not determine executable to run` | Old plugin (< 2026.0.6) launched matter-server via `npx`, which no longer works (the package has no bin). | Upgrade the plugin to ≥ 2026.0.6. |
| `Connect call failed ('127.0.0.1', 5580)` | matter-server isn't running, crashed, or is on a different port. | Check `matter-server.err.log`; confirm the port matches the config; in managed mode re-save the config; in manual mode (re)start matter-server. |
| nvm node not found by the managed agent | Auto-detect didn't find the nvm node. | Set **Node bin directory** explicitly to your own nvm bin dir (see Step 1), or use Homebrew node. |
| `BLE is not enabled on this platform` warning | Expected — matter-server never uses BLE. The out-of-box BLE step belongs to a phone or ecosystem hub; everything this plugin does is over IP, for Wi-Fi and Thread alike. | Benign; ignore. |
| New managed LaunchAgent doesn't take effect | A prior agent with the same label is still loaded. | Plugins ▸ Matter ▸ **Restart matter-server** (it reloads the plist and stops strays), or `launchctl bootout gui/$(id -u)/com.simons-plugins.indigo-matter` then restart the plugin. |
| `Server failed to start [storage-lock] Storage is locked by another process (pid N)` | A second matter-server (orphaned from an earlier LaunchAgent) is still running and holds the storage lock. | The plugin now reaps such strays on start/restart — Plugins ▸ Matter ▸ **Restart matter-server**. If it persists, reboot the Mac (kills the orphan; the lock goes stale and is reclaimed automatically). |
| matter-server won't start after an upgrade (wedged install) | The installed package is damaged or incompatible. | Plugins ▸ Matter ▸ **Reinstall matter-server (clean)…** — deletes `~/indigo-matter/node_modules` and reinstalls fresh. Your commissioned devices are kept (the fabric/storage is untouched). |
| Commissioning fails on device attestation (test/development certificate) | The device presents a test PAA (Homebridge and other dev bridges do); only the production DCL is trusted by default. | Configure… ▸ **Show advanced server settings** ▸ **Allow test/development device certificates**, then **reload the plugin** (or Plugins ▸ Matter ▸ **Restart matter-server**) — saving the config alone does not apply it. The log then shows the "RELAXED device attestation" warning, which reports what the plugin *starts* matter-server with; if commissioning still fails on attestation, confirm the running process really has the flag using the `lsof`/`ps` check in the row below. Managed (local) mode only — for a remote matter-server, add `--enable-test-net-dcl` where you start it. |
| A saved setting doesn't change how matter-server behaves | Settings only reach the server when the plist is regenerated and launchd reloads it. Saving the config does neither. | Reload the plugin, or Plugins ▸ Matter ▸ **Restart matter-server** (it rebuilds from current prefs). Then verify what is actually running rather than trusting the log: `lsof -nP -iTCP:<your port> -sTCP:LISTEN` (5580 unless you changed it), then `ps -ww -o pid=,command= -p <PID>`, and compare that command line against `ProgramArguments` in `~/Library/LaunchAgents/com.simons-plugins.indigo-matter.plist`. |
| Log says the server started, but it behaves like the old one | A matter-server started **outside** this plugin — or with a different `--storage-path` — is holding the port. The plugin's orphan reaper only matches servers using *its* package dir and storage path, so it never stops that one, and the fresh instance dies on the port bind (`EADDRINUSE` in `matter-server.err.log`) while the stray keeps answering on 5580 with its own arguments. (A stray with the *same* storage path fails earlier and differently — see the storage-lock row above.) | Find the owner with `lsof -nP -iTCP:5580 -sTCP:LISTEN`, confirm with `ps -ww -o pid=,command= -p <PID>`, then `kill <PID>`. Note matter-server may exit 0 on that failure, which `KeepAlive` treats as a clean exit, so launchd will not respawn it — use Plugins ▸ Matter ▸ **Restart matter-server** afterwards. |
| Hand-edited plist keeps reverting | The plugin regenerates the plist from config on restart. | Change settings via **Configure…**, not the plist. |

---

## Upgrading matter-server

matter-server is Alpha; test before adopting a new version. With the plugin (or
matter-server) stopped, and after **backing up the fabric** (above):

```bash
npm install --prefix ~/indigo-matter matter-server@<new-version>
```

Then restart the Matter plugin (Mode A) or restart matter-server (Mode B) and re-verify
per Step 4.

---

## Uninstall

Removing the plugin from Indigo also removes the managed LaunchAgent (it boots out and
deletes `~/Library/LaunchAgents/com.simons-plugins.indigo-matter.plist`).

The plugin **never** deletes the storage directory. Your fabric at
`~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server/` stays put — so a
reinstall keeps all your devices. Delete it manually only if you intend a full factory
reset (and re-commission everything).
</content>
</invoke>
