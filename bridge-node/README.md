# indigo-matter-bridge

The Matter **bridge node** for the
[indigo-matter](https://github.com/simons-plugins/indigo-matter) Indigo plugin.

It exports selected [Indigo](https://www.indigodomo.com) devices as Matter
accessories, so an ecosystem sees them as ordinary Matter devices on a bridge.
**Apple Home** is the ecosystem this is built and documented for; Alexa, Google
Home and SmartThings are untested and unclaimed.

> **This package is not useful on its own.** It has no configuration of its own,
> no discovery, and no idea what an Indigo device is. It is one half of a pair:
> the plugin decides *what* to export and the node makes it a Matter accessory.
> Install the Indigo plugin; it installs and manages this package for you, via
> its own **Plugins ▸ Matter ▸ Install/update the Matter bridge** menu
> item and a launchd LaunchAgent it writes.

## Why it is a separate process

matter.js is imported **here and nowhere else**. Indigo plugins are Python, and
the alternative — a Node shim inside the plugin, or a second Matter stack — buys
two versions of the specification and a second place for accessory identity to
drift. The plugin talks to this node over a loopback WebSocket with a versioned
JSON protocol, and refuses to attach on a version mismatch rather than guessing.

The contract is
[`docs/BRIDGE_PROTOCOL.md`](https://github.com/simons-plugins/indigo-matter/blob/main/docs/BRIDGE_PROTOCOL.md)
in the plugin repository. It is a real contract: both sides' test suites assert
against one shared file of golden frames, so a frame that changes on one side
fails the other side's tests.

## Running it by hand

Normally the plugin's LaunchAgent does this. For development:

```sh
node dist/main.js \
  --storage-path ~/Library/Application\ Support/com.simons-plugins.indigo-matter/bridge-node \
  --ws-port 5581 \
  --matter-port 5540 \
  [--mdns-interface en0]
```

Every flag is validated and the node **refuses to start** on a value it cannot
parse, rather than silently running somewhere unexpected — including a flag given
with no value.

| Flag | Meaning |
|---|---|
| `--storage-path` | Where identity, fabrics and the endpoint-number map live. **Sacred**: losing it un-pairs every ecosystem and duplicates every accessory. |
| `--ws-port` | The loopback protocol port the plugin dials (default 5581). |
| `--matter-port` | The Matter UDP port to advertise on (default 5540). |
| `--mdns-interface` | Pin mDNS to one interface. Omit to let matter.js choose; on a Mac with VPN/utun interfaces, pinning is usually necessary. |

The node writes its own log to stdout/stderr. Under the plugin's LaunchAgent that
is `~/Library/Logs/indigo-matter/bridge-node.log` and `bridge-node.err.log`.

## Storage

Two files in `--storage-path`, alongside matter.js's own store:

* **`identity.json`** — install id, commissioning passcode and discriminator,
  plus a witness recording that the bridge has been commissioned at least once.
  An unreadable one is moved aside and a replacement minted **in memory only**:
  the `SerialNumber`/`UniqueID` every paired ecosystem knows are never
  overwritten.
* **`endpoint-map.json`** — the persisted `UniqueID → endpoint number` map. It
  allocates nothing (matter.js owns the numbers); it is an independent *witness*,
  so a lost or reset matter.js store becomes a log line instead of silently
  duplicating every accessory in every ecosystem. Drift is reported and never
  repaired automatically — repairing it would call the same fault clean on the
  next pass.

Back both up. The plugin's **Export fabric backup…** menu item covers them.

## Uncertified by design

The bridge advertises with the specification's **test vendor id** (`0xFFF1`), so
every ecosystem shows an "uncertified accessory" warning when you add it. That is
normal and expected — Homebridge and Home Assistant produce the same warning —
and choosing "Add Anyway" is the intended path. Matter certification is a paid,
per-product process that a self-hosted bridge cannot meaningfully complete.

## Development

```sh
npm install
npm run build      # tsc
npm test           # builds, copies the shared golden frames, runs node --test
```

The test suite includes an integration file that stands up a **real**
`ServerNode` on a real Matter stack behind a real WebSocket server, because the
faults worth catching here (a role factory that builds but writes the wrong
attribute; a bridged child publishing the wrong manufacturer) cannot fail against
a stub.

## Releasing

The package is publish-ready by construction: `files` is limited to `dist`,
`main` is `dist/main.js` (what the plugin's `bridge_agent.DEFAULT_BRIDGE_ENTRY`
expects), `engines.node` is `>=22.13.0`, and a `prepublishOnly` script runs
`clean` then `build` so a stale `dist` can never ship. (`npm pack --dry-run`
also includes `README.md` — npm always ships it regardless of `files`.)

```sh
npm login                 # the package owner's npm account
npm test                  # publishing an untested build is the one unrecoverable mistake
npm publish --access public
```

**Every release bumps two versions in step**, and a mismatch is silent until an
install fails: `package.json`'s `version` here, and
`bridge_agent.DEFAULT_INSTALL_SPEC` in the plugin. `test_bridge_agent.py`
asserts they agree, so a mismatch is a test failure rather than a field report.

**Testing an unreleased node** (dev workaround, *not* the shipped default): the
on-disk layout the LaunchAgent expects is reproduced by a local install —

```sh
npm install --prefix ~/indigo-matter /path/to/indigo-matter/bridge-node
```

which puts the package at `~/indigo-matter/node_modules/indigo-matter-bridge`
exactly where `LaunchAgent._server_entry()` looks for it (`npm link` works
too). The plugin's install menu still tries the registry spec — the local
install covers everything else.

## Licence

MIT. Not affiliated with Perceptive Automation, the Connectivity Standards
Alliance, Apple, Amazon, Google or Samsung.
