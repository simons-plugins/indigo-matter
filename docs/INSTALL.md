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
[`matter-server`](https://github.com/matter-js/matterjs-server), currently **Alpha
v0.6.2**) — which holds the Indigo-owned Matter fabric and exposes a WebSocket control
API. The plugin connects to that WebSocket and translates Matter clusters into
first-class Indigo devices.

A few things to know before you start:

- **Wi-Fi Matter devices only, for now.** Thread first-commission is non-functional in
  matter-server 0.6.2 (it does not supply the Thread network name), and BLE is not
  enabled on macOS. Wi-Fi Matter devices work today.
- **Domio drives commissioning.** You add devices from the **Domio iOS app**, not from
  Indigo. Apple Home commissions the device first; Domio relays a share/pairing code to
  the plugin, and the plugin joins the device's fabric as a second admin over IP (the
  "share model"). The plugin owns runtime control for the device's lifetime.
- **matter-server is Alpha software.** Pin the version, back up the fabric, and expect
  occasional rough edges.

---

## Prerequisites

- **macOS** running your Indigo Server.
- **Indigo 2025.2+** with the `indigo-matter` plugin installed (≥ **2026.0.6** — earlier
  builds launched matter-server via `npx` and no longer work; see Troubleshooting).
- **Node.js 22 LTS**, installed via **nvm** or **Homebrew** (Step 1).
- Network: the Indigo Server and your Wi-Fi Matter devices on the same LAN.

The plugin's only Python dependency (`websockets`) is auto-installed from
`requirements.txt`; if that silently fails, install it manually per the note in that
file.

---

## Step 1 — Install Node.js 22

Pick **one** of the following. The reference server uses nvm with Node 22.

### Option A — nvm (recommended)

```bash
# Install nvm if you don't have it (see https://github.com/nvm-sh/nvm)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
# Restart your shell, then:
nvm install 22
nvm use 22
node --version    # confirm v22.x
```

With nvm, node lives at `~/.nvm/versions/node/vXX/bin`. The plugin auto-detects nvm
(default alias, else newest installed), but you can pin the exact directory in the
plugin config (`nodeBinDir`) if auto-detect picks the wrong one.

### Option B — Homebrew

```bash
brew install node@22
brew link node@22
node --version    # confirm v22.x
```

Homebrew users normally leave `nodeBinDir` blank.

---

## Step 2 — Install matter-server

Install matter-server into a dedicated project directory at `~/indigo-matter`. The
plugin's managed LaunchAgent expects the package at
`~/indigo-matter/node_modules/matter-server`.

```bash
mkdir -p ~/indigo-matter
npm install --prefix ~/indigo-matter matter-server@^0.6.2
```

This creates `~/indigo-matter/package.json` with `"matter-server": "^0.6.2"` — note the
caret: installs float within the 0.6.x series (a fresh install today may give e.g. 0.6.8,
which has been validated in the field). Don't install globally — a project-local install
keeps the version controlled. The project is Alpha and moving fast; stay within 0.6.x,
don't track `main`.

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
2. If you installed Node with **nvm**, set **Node bin directory** to your node bin dir,
   e.g. `~/.nvm/versions/node/v22.22.3/bin`. Homebrew users leave it blank (auto-detect).
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
| **Node bin directory (optional)** | *(blank)* | Directory containing `node`. Blank = auto-detect (Homebrew, then nvm, then PATH). nvm users may pin, e.g. `~/.nvm/versions/node/v22.22.3/bin`. Managed mode only. |
| **matter-server storage path** | `~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server` | The fabric. See Backups. |
| **Manage matter-server LaunchAgent automatically** | off | On = plugin supervises matter-server (Mode A); off = you run it (Mode B). |
| **Primary network interface** | `en0` | macOS interface matter-server binds to. Managed mode only. |
| **matter-server listen address** | `127.0.0.1` | IP the managed server binds its (unauthenticated) control API to. Keep loopback. Managed mode only. See Security. |
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

Once both are healthy, add a Wi-Fi Matter device from the **Domio app** — it will appear
as a native Indigo device.

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
| nvm node not found by the managed agent | Auto-detect didn't find the nvm node. | Set **Node bin directory** explicitly, e.g. `~/.nvm/versions/node/v22.22.3/bin`. |
| `BLE is not enabled on this platform` warning | Expected — BLE isn't used (Wi-Fi only). | Benign; ignore. |
| New managed LaunchAgent doesn't take effect | A prior agent with the same label is still loaded. | `launchctl bootout gui/$(id -u)/com.simons-plugins.indigo-matter`, then restart the plugin. |
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
