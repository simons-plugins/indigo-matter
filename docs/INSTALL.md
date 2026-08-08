# Installing indigo-matter's two Node runtimes

The plugin has two halves, and each one is a **separate Node process with its own
npm package**. This guide covers both:

- **matter-server** — the inbound controller. The plugin drives it so that Matter
  devices in your house become Indigo devices. **Steps 1–4**, and this is what a
  normal install needs. These steps are derived from the live
  managed-LaunchAgent bring-up on the reference Indigo server.
- **the Matter export bridge** — the optional outbound half, which publishes
  selected *Indigo* devices to Apple Home as Matter accessories. **Steps E1–E4**.
  Its npm package (`indigo-matter-bridge`) is on the registry and installs from
  its own menu item — see
  [Exporting Indigo devices](#exporting-indigo-devices-indigo-as-a-matter-bridge).

If you just installed the plugin and Indigo is logging
`Connect call failed ('127.0.0.1', 5580)`, you are in the right place — the plugin is
running but matter-server is not.

## Contents

- [Overview](#overview) — including
  [the Plugins ▸ Matter menu](#the-plugins--matter-menu)
- [Prerequisites](#prerequisites)
- [Step 1 — Install Node.js 22](#step-1--install-nodejs-22)
- [Step 2 — Install matter-server](#step-2--install-matter-server)
- [Step 3 — Configure the plugin](#step-3--configure-the-plugin)
- [Step 4 — Verify](#step-4--verify)
- [**Exporting Indigo devices (Indigo as a Matter bridge)**](#exporting-indigo-devices-indigo-as-a-matter-bridge)
  - [Export prerequisites](#export-prerequisites)
  - [Step E1 — Install the export bridge](#step-e1--install-the-export-bridge)
  - [Step E2 — Export your first device](#step-e2--export-your-first-device)
  - [Step E3 — Open a pairing window](#step-e3--open-a-pairing-window)
  - [Step E4 — Add the bridge in Apple Home](#step-e4--add-the-bridge-in-apple-home-and-expect-the-uncertified-prompt)
  - [The export settings in Configure…](#the-export-settings-in-configure)
  - [Removing an export, unpairing, and stopping the bridge](#removing-an-export-unpairing-and-stopping-the-bridge)
  - [The two destructive recovery actions](#the-two-destructive-recovery-actions)
- [Backups](#backups) — and, for exporters,
  [back up the bridge's storage too](#if-you-export-devices-back-up-the-bridges-storage-too)
- [Security](#security) — and, before exporting,
  [before you pair the export bridge](#before-you-pair-the-export-bridge)
- [Troubleshooting](#troubleshooting) —
  [the controller](#the-controller-matter-server) ·
  [the export bridge](#the-export-bridge)
- [Upgrading matter-server](#upgrading-matter-server)
- [Uninstall](#uninstall)

> Export material deliberately appears in four places: the walkthrough below,
> plus its own subsection under Backups, under Security, and under
> Troubleshooting. The links above are the shortcuts to the three that are not
> under the export heading.

---

## Overview

The `indigo-matter` plugin does not speak Matter directly. It manages a separate
Node.js process — **matter-server** (npm package
[`matter-server`](https://github.com/matter-js/matterjs-server), currently **Beta
1.2.2**) — which holds the Indigo-owned Matter **fabric** and exposes a WebSocket control
API. The plugin connects to that WebSocket and translates Matter clusters into
first-class Indigo devices.

> **Fabric** is the word that recurs throughout this guide. A fabric is one
> controller's trust domain: the set of cryptographic credentials that controller
> installs on a device. A Matter device can belong to several at once — that is
> what lets Apple Home and Indigo both control the same plug, independently. Full
> explanation in
> [MATTER.md → Fabrics and multi-admin](./MATTER.md#fabrics-and-multi-admin-one-device-many-controllers).

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
- **There is a second, optional half.** The plugin can also work the other way round and
  publish selected *Indigo* devices to Apple Home as Matter accessories, from a separate
  bridge process with its own npm package (`indigo-matter-bridge`), installed from
  **Plugins ▸ Matter ▸ Install/update the Matter export bridge**. Nothing is exported
  and no bridge process runs until you add a device to the export list, and it needs
  Node (Step 1) and a storage path (Step 3) — *not* a working matter-server. See
  [Exporting Indigo devices](#exporting-indigo-devices-indigo-as-a-matter-bridge).

### The Plugins ▸ Matter menu

The menu is grouped into five sections, separated by dividers. Both halves of the
plugin have their own install item, and they are **not** interchangeable: they are
separately versioned npm packages, and running the wrong one restarts a half you
did not mean to touch.

| Section | Items | What it covers |
|---|---|---|
| 1 | *Commission device by setup code (advanced)…*, *Decommission Matter device…* | Matter devices Indigo **controls** — the everyday actions |
| 2 | *Manage Matter Exports…*, *Pair Matter Bridge…*, *Unpair an Ecosystem…* | Indigo devices Indigo **publishes** — the everyday actions |
| 3 | *Install/update matter-server*, *Restart…*, *Reinstall (clean)…*, *Open matter-server log…* | The inbound controller's own plumbing |
| 4 | *Install/update the Matter export bridge*, *Reinstall… (clean)…*, *Stop…* | The outbound bridge node's own plumbing |
| 5 | *Back up the Matter fabric…*, *Restore a fabric backup…*, *Rebuild Matter Endpoint Map…*, *Reset Matter Export Pairings…* | Backup and recovery. The last two are destructive — see [the two destructive recovery actions](#the-two-destructive-recovery-actions) |

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

## Exporting Indigo devices (Indigo as a Matter bridge)

> **You cannot install this yet.** The export bridge needs a second npm package,
> `indigo-matter-bridge`, and it **has not been published to the npm registry**.
> Until it is, **Install/update the Matter export bridge** cannot resolve the
> version the plugin pins, and the install fails. Nothing else in the plugin is
> affected — the inbound controller, your commissioned devices and every Indigo
> device carry on as normal — but export cannot be brought up. Everything below
> is what you are waiting for.

Everything above is the plugin's **inbound** half: Matter devices becoming Indigo
devices. The plugin also runs the opposite direction — a selected set of your
*existing* Indigo devices (Z-Wave, Insteon, Zigbee, MQTT, anything a plugin owns)
published outward as Matter accessories on a bridge, so Apple Home can see and
control them. No cloud, no second bridge app.

**This adds a second Node process, and almost everything doubles.** The export
bridge is not matter-server doing extra work. It is its own npm package, its own
launchd job, its own storage directory, its own log files, its own two ports and
its own menu items. One can be broken while the other works, and an export
failure never touches the Matter devices Indigo controls. Wherever this guide
names a log, a port, a backup or a LaunchAgent from here on, check which of the
two it belongs to.

Export is opt-in per device: **nothing is exported and no bridge process runs
until you add a device** to the list in *Manage Matter Exports…*. (The
**Enable Matter export** tickbox in Configure… is on by default. It is a master
*off*-switch, not the thing that starts anything — an empty export list is what
keeps a fresh install inert.) See [MATTER.md](./MATTER.md) → *Indigo as a Matter
bridge* for how it works and what it can and cannot represent; this section is
the setup.

Three things to know before you start:

- **Some of this has been proven on real hardware, and some has not.** What has:
  the bridge **has** been commissioned into Apple Home and the uncertified prompt
  accepted; an on/off light and a dimmer **have** been controlled from Apple Home
  and from Indigo, each direction showing up on the other side; and three
  exported accessories **kept their identities** across a plugin and bridge-node
  upgrade, with no duplicates in Apple Home. What has not: pairing driven from
  the plugin's own menu (the pairing above was driven from the bridge node's
  console), a second ecosystem alongside Apple Home, a full Mac reboot, and the
  managed LaunchAgent end to end. Treat the export half as new.
- **Apple Home is the ecosystem this is built and documented for.** Alexa, Google
  Home and SmartThings are **untested and unclaimed** — the bridge is a standard
  multi-admin Matter bridge and there is no reason they should not work, but
  nobody here has hardware to try them on, so nothing is promised. If you pair
  one, please say so on the forum.
- **The bridge is not certified**, and every ecosystem will say so when you add
  it. That is expected, not a fault — see
  [Step E4](#step-e4--add-the-bridge-in-apple-home-and-expect-the-uncertified-prompt).

### Export prerequisites

- **Node.js ≥ 22.13.0** — the same Node you installed in Step 1. The bridge runs
  on it, and the plugin resolves it exactly as it does for matter-server
  (auto-detect, or the **Node bin directory** you pinned).
- **A storage path** (Step 3). The bridge's own storage directory is *derived*
  from the controller's — it is the `bridge-node/` folder beside it — so the
  **matter-server storage path** field decides where the bridge keeps its
  ecosystem pairings and its endpoint map, whether or not you run matter-server.
- **A second npm package**, `indigo-matter-bridge`. It is not part of the plugin
  bundle — the plugin ships no JavaScript — and it is not the same package as
  `matter-server`. It is installed from its own menu item (Step E1), once it is
  published.

What export does **not** need is a working matter-server. The two are separate
processes with separate packages, ports, storage directories and launchd jobs,
and nothing in the export path talks to the controller. So if you only want to
publish Indigo devices outward, Step 2 is not yours — Steps 1 and 3 are. The
plugin will go on reporting that it cannot reach matter-server, which is log
noise in that configuration, not an export fault.

### Step E1 — Install the export bridge

**Plugins ▸ Matter ▸ Install/update the Matter export bridge.**

It installs into the same `~/indigo-matter` npm root as matter-server, with the
same Node, and takes a minute; watch the Event Log. On success you will see:

```
Matter export bridge installed. It is NOT being started: nothing is exported yet,
and the bridge only runs while the export list is non-empty.
```

That is correct — the bridge process starts itself the moment you export
something, and stops again when you stop exporting. **Do this step before Step
E2**: exporting a device with the package missing writes no LaunchAgent and logs
an error pointing you straight back here.

### Step E2 — Export your first device

**Plugins ▸ Matter ▸ Manage Matter Exports…**

The dialog is a picker plus a detail pane, and it has no Execute button — the
buttons inside it do the work, and **Close** when you are done.

1. **Filter by name** (optional), then click **Apply filter / refresh**. The
   picker lists **up to 300 matching Indigo devices** — past that a tail row says
   so, and narrowing the filter is how you reach the rest. A `●` marks one that
   is already exported. Devices that cannot be exported are listed too, reading
   *"— not exportable: …"* **with the reason**, rather than being quietly
   omitted and left to hunt for. The one exception is devices this plugin
   created itself: those are absent altogether, because a Matter device is never
   re-exported over Matter.
2. **Pick a device.** Its saved settings load, or sensible defaults appear.
3. **Export as** — the role. Indigo cannot tell a plug from a lamp from a lock,
   so you declare it; the first option in the menu is always the safest default.
   Only roles that device can legitimately take are offered.
4. **Name in ecosystems** (optional) — leave empty to use the Indigo device name.
5. **Invert position** — window coverings only. Matter's convention is 100 % =
   fully open; tick this if your Indigo device is the other way round.
6. **Add / update export.**

The **Status** line reports what happened, and **Currently exported** lists the
allow-list. Behind that one click — on the empty-to-non-empty transition — the
plugin writes the bridge node's LaunchAgent, starts it, and connects. The log
should show:

```
Matter export: bridge node LaunchAgent is running (protocol port 5581, Matter port 5540)
Matter export: bridge node attached — 1 endpoint(s) live, not yet paired
```

<!-- SCREENSHOT WANTED: the "Manage Matter Exports…" dialog with a device
     selected, showing the role menu open and an excluded device visible in the
     picker with its reason. -->

> **You only pair the bridge once per ecosystem.** There is one bridge
> accessory, and every export is an endpoint on it — so devices you export
> *later* appear in every already-paired ecosystem automatically, with no action
> from you. Do **not** open a fresh pairing window to add a device: a pairing
> window is a live credential that exposes everything you export to anyone who
> can read it (see [Security](#before-you-pair-the-export-bridge)). You open one
> only to add a *new ecosystem*.

> **Changing a device's role later re-creates the accessory.** Matter does not
> allow an endpoint to change device type, so the plugin removes it and adds it
> back — and every paired ecosystem treats that as a brand-new accessory, losing
> the name, room, scenes and automations you gave it there. The dialog says so
> when you do it.

### Step E3 — Open a pairing window

**Plugins ▸ Matter ▸ Pair Matter Bridge…** → **Open pairing window**.

Read [Before you pair the export bridge](#before-you-pair-the-export-bridge)
under **Security** first. In short: the pairing code is a live credential, the
page that shows it is only password-protected if you have turned Indigo Web
Server authentication on, and anyone who reaches either can add *your* exported
devices to *their* Apple Home account.

- The window lasts **180–900 seconds** (3–15 minutes); 900 is the default and the
  maximum. A value outside that band is rejected in the dialog rather than
  silently changed.
- You must have exported something first. The bridge only runs while the export
  list is non-empty, so with nothing exported the dialog answers *"Export at
  least one device in 'Manage Matter Exports…' first"*.
- **The code goes to the Event Log**, not into the dialog: Indigo dialogs cannot
  display a value their own button computes. The log line carries the manual
  pairing code, the raw `MT:` QR payload, the expiry, and the URL of a page that
  shows the same things.
- Open that URL on the phone you are about to pair with. The page shows the
  manual code large enough to read across a room, the payload for copying, and a
  link to the Matter project's own QR viewer for anyone who would rather scan.
  **No QR image is generated locally** — that link sends the payload to a
  third-party page and needs internet access, which the page says on itself.

A Matter passcode is **not durable**. Once the first ecosystem commissions the
bridge, that code is dead; every further ecosystem needs a fresh window from this
same menu item. That is why it is called "open a pairing window" and not "show
the code".

<!-- SCREENSHOT WANTED: the pairing page as it renders on an iPhone. -->

### Step E4 — Add the bridge in Apple Home, and expect the uncertified prompt

In the Home app, add an accessory, take the option to enter a code by hand, and
type the manual pairing code from the Event Log — eleven digits, and the only
thing you need. Apple Home finds the accessory as **Indigo Matter Bridge** — and
then warns you that it is an **uncertified accessory**.

**That warning is expected. Choose "Add Anyway".**

Read what the warning actually asserts. It does not say the accessory may be
hostile, or that Apple has found anything wrong with it. It says that nobody has
paid the Connectivity Standards Alliance to certify this product — that is the
entire claim. Matter device attestation runs one way, the *commissioner*
checking the *device's* certificate against the CSA's list, so the trust policy
here belongs to Apple, not to Indigo. The bridge presents a development/test
attestation certificate, which is exactly what **Homebridge**, **matterbridge**
and **Home Assistant's** own bridge present, and shipping uncertified is the
normal state for this class of software: a CSA certificate requires paid
membership plus per-product certification, which is not proportionate for a free
community plugin. The plugin can neither suppress the prompt nor work around it;
it is uncertified in *both* directions and says so in the README.

Once you accept it, every device on your export list appears in Apple Home as an
accessory of the role you declared.

<!-- SCREENSHOT WANTED: Apple Home's "Uncertified Accessory" sheet with the
     Add Anyway button, taken while adding the Indigo bridge. -->

> **Apple takes two fabric slots, not one.** An Apple Home pairing creates an
> *Apple Home* fabric **and** an *Apple Keychain* fabric (iCloud Keychain sync).
> Both will appear in the plugin's readouts and in the unpair picker; that is
> normal, and neither is a duplicate you should remove.

Re-open **Plugins ▸ Matter ▸ Configure…** afterwards and the **Status** line in
the Matter export section names what you are paired with.

### The export settings in Configure…

| Field | Default | Purpose |
|---|---|---|
| **Enable Matter export** | on | The wholesale switch. Unticking it stops the bridge and its accessories go unavailable — it does **not** unpair anything or delete accessories, so ticking it again brings them straight back. Nothing runs until at least one device is exported. This is the one export setting that acts immediately on save. |
| **Status** | *(read-only)* | How many devices are exported, whether the plugin is connected to the bridge node, which ecosystems it last reported being paired with, and whether a pairing window is open. Computed when the dialog opens, from what the plugin already knows — it never blocks on the bridge. |
| **Bridge control port (loopback)** | `5581` | How the plugin talks to the bridge node. Loopback only; nothing outside this Mac can reach it. Change only if something else already uses 5581. |
| **Matter port** | `5540` | The UDP port ecosystems reach the bridge on. 5540 is the Matter default. Move it only if another Matter stack on this Mac already holds it (see Troubleshooting). |

**Changing either port needs a plugin reload** — they are read when the bridge
node and its LaunchAgent are built. The **Matter port** field says so in its own
description; the **Bridge control port** field does not, but the same is true
of it.

### Removing an export, unpairing, and stopping the bridge

| You want to | Do this | What happens |
|---|---|---|
| Stop exporting one device | *Manage Matter Exports…* → pick it → **Remove export** | Its endpoint goes, and the accessory disappears from every paired ecosystem. Your pairings stand. |
| Stop exporting everything | Remove every export | All the endpoints go, the bridge node is stopped and its LaunchAgent removed (so a reboot cannot bring it back), and **your pairings are kept**. Re-export anything and it all comes back. |
| Pause export without unpairing | *Configure…* → untick **Enable Matter export** | The bridge stops and accessories show as unavailable. Nothing is unpaired, nothing is deleted, and the allow-list is untouched. |
| Remove one ecosystem | *Plugins ▸ Matter ▸ Unpair an Ecosystem…* | Every accessory Indigo exports leaves **that** ecosystem, with the names, rooms, scenes and automations you built on them there. Other ecosystems are unaffected — **but if it is your only one, this factory-resets the whole bridge** (see below the table). Two confirmation boxes. |
| Get the bridge process gone right now | *Plugins ▸ Matter ▸ Stop the Matter export bridge…* | Stops the node and removes its LaunchAgent. Pairings and export list are unchanged; accessories go unavailable until the next export change starts the bridge again by itself. Mainly for when you are about to disable the plugin, which would otherwise leave the node running with nothing supervising it. |

Two notes on unpairing. The picker names the *vendor* and the fabric index
(`Apple Home (index 1)`) because that is what the bridge removes a pairing by,
and it is built from the last state the bridge reported — so an ecosystem that
dropped the bridge since you opened the dialog may still be listed. And
**unpairing the last remaining ecosystem resets the whole bridge**: Matter's own
rules mean the node factory-resets itself when its fabric set empties, so it
starts advertising for commissioning again, exactly as if you had used *Reset
Matter Export Pairings…*.

### The two destructive recovery actions

Both are in the plugin menu, both make you tick a box (or two), and neither is
something you should reach for casually. They are described here in the same
words the dialogs use.

**Rebuild Matter Endpoint Map… — the way out of an unreadable map.** Use it
**only** when the plugin says the bridge node is refusing to export because its
endpoint-number map is unreadable. The map is the bridge's safety record of
which Matter accessory number belongs to which Indigo device; rebuilding it
discards the unreadable record and adopts whatever numbers exist now as the new
one. It renumbers nothing, so **the rebuild itself cannot duplicate
accessories** — what you see afterwards depends on which fault brought you
here:

- *only the map file was damaged* — the accessories keep their numbers, no
  paired ecosystem sees any further change, and the rebuild simply clears the
  refusal;
- *the bridge's Matter storage was lost* — every paired ecosystem is already
  holding accessories under the old numbers. Those are dead: you have to delete
  them by hand, and any names, rooms, scenes and automations built on them are
  lost. That damage was done by the storage loss; rebuilding accepts it rather
  than causing it.

The unreadable file is kept as `endpoint-map.json.corrupt-<timestamp>` in the
bridge storage folder; if you have a backup of the original, restoring that is
better than rebuilding. No pairing is touched and no Indigo device is changed.
If the bridge is **not** refusing, the plugin refuses the rebuild: there is no
fault to recover from, and a rebuild would throw away the retained endpoint
numbers of every device you are not currently exporting — the ones that make
re-adding a device restore the same accessory rather than create a second one.

**Reset Matter Export Pairings… — recoverable by pairing again.**
It *removes **every** ecosystem pairing from the Matter export bridge and starts
advertising for commissioning again.* Apple Home, and anything else you have
paired, lose all of the accessories Indigo exports, and you have to pair the
bridge again from scratch. This is the export side only: Matter devices that
Indigo **controls** are not affected, and no Indigo device is changed or deleted.
Endpoint numbers are kept, so re-pairing the same ecosystems restores the same
accessory identities where it can. Use it when you want a clean start on the
pairing side, or when the bridge's pairings are in a state you can't otherwise
clear.

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

Use **Plugins ▸ Matter ▸ Back up the Matter fabric…**. It writes a timestamped zip into a
`backups/` directory beside the storage dir and prunes old ones, and
**Restore a fabric backup…** puts one back (moving the current fabric aside first, so a
bad restore is reversible). Copy the zips somewhere off-machine as well — a backup on
the same disk is not a backup.

> **Restore has a precondition, and its dialog states it: *"Requires 'Manage
> LaunchAgent' to be on."*** Restoring stops matter-server, swaps the storage
> directory and starts it again, so it needs the plugin to be the thing that
> runs matter-server. In Mode B (manual) the restore is refused — stop
> matter-server yourself and swap the directory by hand instead.

If you would rather do it by hand, stop the plugin (or matter-server) and copy the
directory:

```bash
cp -R \
  "$HOME/Library/Application Support/com.simons-plugins.indigo-matter/matter-server" \
  "$HOME/Backups/indigo-matter-fabric-$(date -u +%Y%m%d)"
```

### If you export devices, back up the bridge's storage too

The export bridge keeps its own directory, beside the controller's:

```
~/Library/Application Support/com.simons-plugins.indigo-matter/bridge-node/
```

It is every bit as sacred, for two separate reasons. It holds the credentials of
every ecosystem the bridge has been paired with — losing them un-pairs the lot —
and the record of which Matter accessory number belongs to which Indigo device.
Losing *that* is the number-one real-world cause of duplicated accessories: every
ecosystem re-creates every accessory, and their names, rooms and automations go
with the old ones.

**Back up the Matter fabric…** includes this directory in the zip. **Restore does not
yet put it back** — it reports the bridge files it found and skips them, because
restoring them safely needs the bridge node stopped and that wiring is a
follow-up. So for now, if you need to restore the bridge side, copy
`bridge-node/` out of the zip by hand with the bridge stopped (*Stop the Matter
export bridge…*). Your export **list** is not in here at all — it lives in the
plugin's preferences and rides along with Indigo's own database backup.

---

## Security

matter-server's WebSocket control API is **unauthenticated**, and with no
`--listen-address` it binds **all** interfaces — meaning anyone on your LAN could control
your entire Matter fabric.

The plugin defaults the managed server to `--listen-address 127.0.0.1` (loopback) via the
**matter-server listen address** config field. **Keep it on loopback** unless
matter-server runs on a different host and you have a trusted firewall in front of it.

### Before you pair the export bridge

**Turn on Indigo Web Server authentication first.**

*Plugins ▸ Matter ▸ Pair Matter Bridge…* opens a Matter commissioning window and
writes a **live pairing passcode** to the Event Log, together with the URL of a
page that shows the same code and its QR payload. That page is served by the
Indigo Web Server, which asks for a password **only if you have switched
authentication on**.

While the window is open — up to 15 minutes — anyone who can reach that URL, or
who reads that code, can add the bridge to **their own** Apple Home, Alexa or
Google account. What they get is every Indigo device you export: they can see its
state and they can operate it. Removing them afterwards means *Plugins ▸ Matter ▸
Unpair an Ecosystem…*, and you would first have to notice.

So, in order:

1. Turn on authentication in Indigo's Web Server preferences (Indigo ▸
   Preferences ▸ Web Server), or make sure the reflector is the only route in.
2. Only then open a pairing window.
3. Do not paste the pairing URL or the manual code into a chat, an email or a
   forum post — treat both as the credential they are.
4. Pair the ecosystem you meant to, and let the window close (it expires by
   itself; it also closes the moment an ecosystem completes commissioning).

The plugin cannot enforce any of this: authentication belongs to the Indigo Web
Server, and a second password scheme underneath it would be worse than none. It
does say so on the page and in the log line.

---

## Troubleshooting

### The controller (matter-server)

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

### The export bridge

The bridge node keeps its own logs, separate from matter-server's:

```
~/Library/Logs/indigo-matter/bridge-node.log
~/Library/Logs/indigo-matter/bridge-node.err.log
```

Those files are **appended to and never truncated**, so check the timestamps — the
last lines can be from a crash loop days ago that has since been fixed. The plugin
quotes their tail into the Indigo log when it cannot reach the node.

Everything below fails **export only**. Your Indigo devices, and the Matter devices
Indigo controls, are never affected by any of it.

**What you can see.** Symptoms that show up in an ecosystem app or in the export
dialog, most-likely-first — the top two are what a first export usually hits.

| Symptom | Cause | Fix |
|---|---|---|
| You exported a device and **nothing appeared** in Apple Home | The bridge has never been paired. Exporting publishes a device *on* the bridge; an ecosystem sees nothing at all until the bridge itself has been added to it. | Do Step E3 then Step E4. You do this **once per ecosystem** — after that, later exports appear by themselves with no further pairing. |
| Apple Home's **Add Accessory** cannot find the bridge | One of three: nothing is exported (the bridge only runs while the export list is non-empty), or no pairing window is open, or **Primary network interface** in Configure… names an interface this Mac is not actually using. That field is shared with matter-server and the bridge pins its mDNS advertising to it, so a wrong value advertises where nobody is listening. Matter discovery is also mDNS + link-local IPv6, so the phone must be on the same subnet as the Mac. | Export at least one device; open a window with **Pair Matter Bridge…**; check the interface with `ifconfig \| grep "status: active"` and **reload the plugin** after changing it; put the phone on the same subnet/VLAN as the Indigo Mac. |
| An accessory shows as **unavailable** in Apple Home | Either the Indigo device is disabled or unconfigured (the bridge deliberately marks it unreachable rather than letting the ecosystem time out), or export is switched off, or the bridge node is not running. | Re-enable the Indigo device; or Configure… ▸ **Enable Matter export**; or work the log rows below. |
| Accessories **stay** in Apple Home after you stopped exporting them | The removal never reached the node — it was down, or the plugin was reloaded mid-request. | It is recorded and finished automatically the next time the plugin connects to the node: export a device again, or reload the plugin. There is no retry loop in between, so nothing happens before then. |
| A thermostat or sensor exports numbers that look wrong | Indigo records no units anywhere in its device model, so the bridge takes each reading as already being in the unit Matter wants (°C, %RH, lux, m³/h). A °F thermostat therefore exports the Fahrenheit number labelled as Celsius. **Barometric pressure is the one exception**: Indigo's convention is hPa, and the bridge divides by 10 to reach Matter's kPa — so a barometer reading in hPa is already correct. Do **not** "fix" it to report kPa; that is what produces a reading ten times too high. | Not fixable from Indigo's device model today — it needs a declared unit per export. Export devices that already read in metric (and leave barometers in hPa), or leave that one un-exported. |
| A **pressure or flow** sensor never appears in Apple Home | Apple Home has no UI for those Matter device types. | **Nothing is broken and there is nothing to fix.** The accessories exist on the bridge and other ecosystems can use them; Apple simply does not render them. If Apple Home is all you run, don't export those sensors. |
| Apple Home refuses to change an exported accessory's **room**, or every accessory jumps back into the bridge's own room after a restart | **Fixed in plugin 2026.8.4 / bridge `0.6.0`** (issue #141). Older bridges came online with an empty accessory list and only filled it in when the plugin connected, seconds later. Apple reconnects inside that window, reads an empty list, concludes every accessory has gone — and treats the ones that reappear as brand-new accessories, in the bridge's own room, with metadata it will not let you edit. Every restart of the bridge (reboot, plugin upgrade, crash recovery) cost you the room assignments for every exported device. The bridge now rebuilds its accessory list from disk *before* it goes online, so Apple sees accessories that were briefly unavailable rather than accessories that vanished. | **Update the plugin and the bridge node**, then set the rooms once more; they stick from then on. Note that the rooms broken by an *earlier* version are still broken — Apple will not let them be edited — so on this one occasion you still need the older remedy: remove the bridge from Apple Home and pair it again (Step E3 + E4), then assign rooms. On versions before 2026.8.4 that unpair/re-pair is the only remedy, and it has to be repeated after every restart. |

**What the log says.** Verbatim strings from the Indigo Event Log and
`bridge-node.err.log`.

| Log line | Cause | Fix |
|---|---|---|
| `Matter export: the bridge node is not responding after N attempts on port 5581` | The node is not running, is crash-looping, or was never installed. All three look identical at the socket, so the plugin appends what the LaunchAgent found. | Read the sentence that follows it in the log — it names the real cause. Then: install the package (below), free the port (below), or check `bridge-node.err.log`. |
| The log names **Install/update the Matter export bridge** | The `indigo-matter-bridge` package is not installed, so no LaunchAgent was written. This is the normal first-run state. | Run that menu item. Note it cannot succeed until the package is published to npm — see *Export prerequisites*. |
| An `EADDRINUSE` / address-already-in-use failure in `bridge-node.err.log`. The node labels which startup step failed, so the line says whether it was the Matter port or the loopback protocol port | **Matter port (UDP 5540):** another Matter *device* stack on this Mac already holds it — Homebridge 2.x, matterbridge, or a Home Assistant container in host mode. **Protocol port (TCP 5581):** an orphaned bridge node from an earlier LaunchAgent is still running, or an unrelated service has the port. | **Matter port:** stop the other stack, or Configure… ▸ **Show export port settings** ▸ **Matter port**, pick another, and **reload the plugin**. Find the holder with `lsof -nP -iUDP:5540`. Moving off 5540 is a real trade — it is the port ecosystems expect. **Protocol port:** **Stop the Matter export bridge…** then re-save an export (the plugin reaps its own orphans on start). If something else holds it, find it with `lsof -nP -iTCP:5581 -sTCP:LISTEN`, and either stop it or change **Bridge control port** and reload the plugin. |
| `Matter export: the bridge node's LaunchAgent did not start` / `could not be loaded by launchd` | launchd accepted the job and the process died, or refused the plist. | Check `bridge-node.err.log` first. Then `launchctl print gui/$(id -u)/com.simons-plugins.indigo-matter.bridge`. **Stop the Matter export bridge…** followed by re-saving an export rebuilds the plist from scratch. |
| `Matter export: the bridge node is serving NOTHING because its endpoint-number map is unreadable` | The bridge's `endpoint-map.json` is present but corrupt. It refuses to serve rather than silently renumber every accessory. | **Rebuild Matter Endpoint Map…** — and read what it does first (above). The unusable file is kept as `endpoint-map.json.corrupt-<timestamp>` in the bridge storage folder; if you have a backup of the original, restoring it is better than rebuilding. |
| `…because its identity file is unreadable` | `identity.json` is corrupt. This is a *different* fault with a different remedy, and the node refuses a rebuild for it. | Restore or repair `identity.json.unreadable-<timestamp>` from the bridge storage folder and restart the bridge. **Deleting it starts a brand-new bridge**, which every paired ecosystem sees as a different device. |
| The node reports it was commissioned but its Matter fabric storage is gone | The bridge storage directory was moved, restored partially, or lost. | Restore the whole `bridge-node/` directory from a backup. If you have none, accept the loss: reset the pairings and pair each ecosystem again. |
| `Matter export: endpoint-number DRIFT detected` | **Most likely: you have just reset the pairings, or unpaired the last ecosystem.** Both factory-reset the bridge, which wipes matter.js's own endpoint allocation — so the numbers are handed out afresh and no longer match the preserved map. That is *expected*, and saying so is exactly what the preserved map is for. **Otherwise:** storage loss on the bridge side, in which case exported accessories may have swapped identities in paired ecosystems. | **After a reset or a last-ecosystem unpair: no action.** The map is reporting what it exists to report, and re-pairing proceeds normally. **Otherwise:** drift is never repaired automatically, deliberately — an auto-repair would bless the loss and hide the next one. Check the named accessories in each ecosystem, and restore the bridge storage from a backup if you have one. |
| `Matter export: the bridge client is HALTED` | Either the node speaks a different protocol version than this plugin (an old node left running across a plugin upgrade), or it refused a request that would have removed every accessory at once. | Nothing retries on its own. For version skew: **Stop the Matter export bridge…**, then **Install/update the Matter export bridge**, then re-save an export. The reason is on the line itself. |
| `Matter export: the pairing window has expired without an ecosystem completing commissioning` | Nobody paired within the window (up to 15 minutes). The code it showed is now dead. | Open a new one: **Pair Matter Bridge…**. Have the phone in your hand first. |
| The pairing menu says the bridge is *already advertising with its original code* | The bridge has never been paired, so its first commissioning window is already open with its own code — deriving a new one would kill a code you may already be typing. | Use the code in the log. This is not an error. |
| `Matter export: device N is in the export list but will NOT be bridged` | That Indigo device was deleted, disabled beyond recognition, re-typed, or no longer offers the role you exported it as. The rest of the export list is unaffected. | Open **Manage Matter Exports…**, pick the device, and either re-choose a role or **Remove export**. |
| `Matter export: … stopped reporting <state>` | The Indigo device has gone quiet — a flat battery, a plugin that lost it. Matter has no way to say "I no longer know", so the ecosystem keeps showing the last value it was given, indefinitely. | Fix the underlying device. Note this warning is said **once**, when it happens; it is not repeated after a plugin reload, so its absence is not evidence that everything is reporting. |
| `Matter export: '<command>' for device N has not returned after 30s` | Something you pressed in Apple Home reached an Indigo device that is not answering. Commands run one at a time, so everything behind it is queued. | Fix that device or its plugin. The ecosystem already shows the command as done and nothing corrects that until the device next reports. |

---

## Upgrading matter-server

matter-server is Beta; test before adopting a new version. With the plugin (or
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

The same is true of the export bridge. Its LaunchAgent
(`~/Library/LaunchAgents/com.simons-plugins.indigo-matter.bridge.plist`) goes with the
plugin, and its storage at
`~/Library/Application Support/com.simons-plugins.indigo-matter/bridge-node/` stays —
so a reinstall keeps your ecosystem pairings and your accessories' identities. If you are
leaving for good, unpair the bridge from each ecosystem *before* you remove the plugin,
or you will be deleting a dead "Indigo" bridge from each ecosystem's app by hand
afterwards.
