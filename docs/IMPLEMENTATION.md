# `indigo-matter` Implementation Notes

**Status:** Reference for build
**Last updated:** 2026-05-15
**Companion documents:** [`PRD-indigo-matter-plugin.md`](./PRD-indigo-matter-plugin.md), [`API.md`](./API.md)

This document fills the implementation-detail gaps left by the PRD. It is not exhaustive — the PRD is still the source of truth for what to build — but it pins down the specific protocols, file layouts, and worked examples a coding agent needs to avoid making inconsistent assumptions.

## 1. `matterjs-server` Setup

### 1.1 Pinned versions

- Node.js: **22 LTS** (latest patch). Verify `node --version` reports >= 22.0.0.
- **npm package name is `matter-server`** (unscoped), *not* `@matter-js/matterjs-server`. The GitHub repo is `matter-js/matterjs-server`, but the published npm package is the unscoped `matter-server`.
  - Latest as of writing: **v0.6.2 (April 2026), still labelled Alpha** by the project (not Beta as previously stated). Pin to a specific version; do not track `main`.
  - **Before starting M1, the implementing agent must check the latest release at https://github.com/matter-js/matterjs-server/releases (and the npm page for `matter-server`) and pin to that.**

### 1.2 Install

```bash
# Node via Homebrew
brew install node@22
brew link node@22
node --version    # confirm v22.x

# Install matter-server into a dedicated directory (rather than global)
mkdir -p ~/indigo-matter && cd ~/indigo-matter
npm init -y
npm install matter-server@0.6.2

# Create storage directory
mkdir -p "$HOME/Library/Application Support/com.simon.indigo-matter/matter-server"

# Smoke test
npx matter-server \
  --storage-path "$HOME/Library/Application Support/com.simon.indigo-matter/matter-server" \
  --primary-interface en0

# Should listen on http://localhost:5580 (dashboard) and ws://localhost:5580/ws
```

The `--primary-interface en0` flag is explicitly recommended on macOS by the matter-server
README. Adjust `en0` to whatever your actual primary interface is
(`ifconfig | grep "status: active"` will show it).

Don't use `npm install -g`. The project is moving fast and a project-local install pins
the version cleanly.

### 1.3 Storage path

Per ADR §4.3:

```
~/Library/Application Support/com.simon.indigo-matter/matter-server/
```

This directory holds:
- Fabric root CA private key and certificate.
- Per-node operational data.
- matterjs-server's internal config.

**This directory is critical.** Losing it loses the fabric and all device pairings. Plugin install scripts MUST NOT delete it on uninstall; only on explicit user-confirmed factory reset.

### 1.4 LaunchAgent plist (recommended approach — process management approach PM-B per PRD §4.2)

Path: `~/Library/LaunchAgents/com.simon.indigo-matter.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.simon.indigo-matter</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/npx</string>
        <string>--prefix</string>
        <string>/Users/USERNAME/indigo-matter</string>
        <string>matter-server</string>
        <string>--port</string>
        <string>5580</string>
        <string>--storage-path</string>
        <string>/Users/USERNAME/Library/Application Support/com.simon.indigo-matter/matter-server</string>
        <string>--primary-interface</string>
        <string>en0</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/USERNAME/Library/Logs/indigo-matter/matter-server.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/USERNAME/Library/Logs/indigo-matter/matter-server.err.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

If `/opt/homebrew/bin/npx` doesn't exist on this machine (Intel Mac), use
`/usr/local/bin/npx` instead. Verify with `which npx` after install.

The plugin's first-run installer:

1. Creates the storage path and logs directory.
2. Writes the plist with the correct `USERNAME` substituted.
3. Loads it: `launchctl load ~/Library/LaunchAgents/com.simon.indigo-matter.plist`.

Plugin uninstall:

1. `launchctl unload ~/Library/LaunchAgents/com.simon.indigo-matter.plist`.
2. Remove the plist.
3. **Do NOT remove the storage path.** Leave it for re-install or manual cleanup.

### 1.5 Health check

```bash
# Confirm server responding
curl -fsS http://localhost:5580/ -o /dev/null && echo "OK"

# WebSocket smoke test (using wscat: npm install -g wscat)
wscat -c ws://localhost:5580/ws
> {"message_id":"1","command":"server_info","args":{}}
< {"message_id":"1","result":{...}}
```

The dashboard at `http://localhost:5580` is genuinely useful during development — it lets
you commission devices, browse nodes, inspect attribute values, and change log level at
runtime. Use it to cross-check the WebSocket boundary while building the plugin.

## 2. matterjs-server WebSocket Protocol

Reference: https://github.com/matter-js/matterjs-server/blob/main/docs/websockets_api.md (or the matterjs-server fork's equivalent — verify path before starting M1).

The protocol is drop-in-compatible with python-matter-server's WebSocket API.

### 2.1 Connection

- URI: `ws://localhost:5580/ws`
- Subprotocol: none required.
- The server sends a `server_info` event on connect; plugin should wait for this before sending commands.

### 2.2 Message envelope

**Commands (client → server):**

```json
{
  "message_id": "<unique string per request>",
  "command": "<command_name>",
  "args": { ... }
}
```

**Responses (server → client, in reply to a command):**

```json
{
  "message_id": "<same as request>",
  "result": { ... }
}
```

or on error:

```json
{
  "message_id": "<same as request>",
  "error_code": <int>,
  "details": "<human-readable>"
}
```

**Events (server → client, unsolicited):**

```json
{
  "event": "<event_name>",
  "data": { ... }
}
```

### 2.3 Commands the plugin uses

| Command | Purpose | Example args |
|---|---|---|
| `server_info` | Get server version, fabric info | `{}` |
| `get_nodes` | List all commissioned nodes | `{}` |
| `get_node` | Get full node detail (clusters, endpoints, attribute values) | `{ "node_id": 1 }` |
| `commission_with_code` | Commission a new device | `{ "code": "MT:..." }` or `{ "code": "12345678901", "network_only": true }` |
| `open_commissioning_window` | Open a commissioning window on a known node | `{ "node_id": 1 }` |
| `interview_node` | Re-read descriptors from a node | `{ "node_id": 1 }` |
| `remove_node` | Decommission and remove from fabric | `{ "node_id": 1 }` |
| `read_attribute` | One-shot read | `{ "node_id": 1, "endpoint": 1, "cluster": 6, "attribute": 0 }` |
| `write_attribute` | One-shot write | `{ "node_id": 1, "endpoint": 1, "cluster": 6, "attribute": 16, "value": 30 }` |
| `device_command` | Invoke a Matter cluster command | `{ "node_id": 1, "endpoint": 1, "cluster": 6, "command": "On", "args": {} }` |
| `start_listening` | Subscribe to all events | `{}` |

### 2.4 Events the plugin handles

| Event | Trigger | Plugin response |
|---|---|---|
| `node_added` | New node commissioned | If this matches an in-flight `commission_with_code`, advance the job to `reading_descriptors`. Create Indigo devices. |
| `node_removed` | Node decommissioned | Mark Indigo device(s) deleted or unreachable per user-config policy. |
| `node_updated` | Node interview re-ran or capabilities changed | Re-sync Indigo device state and types. |
| `node_event` | Wraps Matter device events | Map to Indigo events / triggers. |
| `attribute_updated` | An attribute we subscribed to changed | Dispatch to cluster handler → update Indigo state. |
| `server_shutdown` | Server going down | Close WS cleanly; mark all devices unreachable; reconnect. |

### 2.5 Example: commission and read

```
→ {"message_id":"1","command":"commission_with_code","args":{"code":"12345678901"}}
← {"message_id":"1","result":{"node_id":42}}

← {"event":"node_added","data":{"node_id":42,...}}

→ {"message_id":"2","command":"get_node","args":{"node_id":42}}
← {"message_id":"2","result":{
     "node_id":42,
     "attributes":{
       "1/0x001D/0x0000": [{"cluster":29,"type":22,"revision":2}],
       "1/0x0006/0x0000": false,
       ...
     },
     "endpoints":{
       "1":{"device_types":[{"device_type":266,"revision":2}],"clusters":[6,29,3]}
     }
   }}

→ {"message_id":"3","command":"device_command","args":{
     "node_id":42,"endpoint":1,"cluster":6,"command":"On","args":{}
   }}
← {"message_id":"3","result":null}
```

Attribute key format `<endpoint>/<clusterId>/<attributeId>` — both cluster and attribute can be hex (`0x0006`) or decimal (`6`); matterjs-server accepts both, normalises in responses.

### 2.6 Cluster ID reference (v1 scope)

| Cluster | ID (hex) | ID (dec) |
|---|---|---|
| OnOff | 0x0006 | 6 |
| LevelControl | 0x0008 | 8 |
| ColorControl | 0x0300 | 768 |
| TemperatureMeasurement | 0x0402 | 1026 |
| RelativeHumidityMeasurement | 0x0405 | 1029 |
| OccupancySensing | 0x0406 | 1030 |
| BooleanState | 0x0045 | 69 |
| IlluminanceMeasurement | 0x0400 | 1024 |
| Thermostat | 0x0201 | 513 |
| FanControl | 0x0202 | 514 |
| Descriptor | 0x001D | 29 |
| Identify | 0x0003 | 3 |
| BasicInformation | 0x0028 | 40 |

### 2.7 Known Alpha-state limitations of matter-server (as of v0.6.2)

The matter-server README explicitly notes the following caveats that affect plugin scope
and milestone planning:

- **Thread commissioning is currently non-functional in matter-server.** Per the README:
  "For Thread Matter.js currently requires a network Name which is not provided." This
  means Matter-over-Thread devices cannot be commissioned via this path until upstream
  resolves it. Wi-Fi Matter devices are unaffected.
- **BLE commissioning is off by default.** Enable with `--ble`. Not needed in this
  architecture because Domio handles BLE through `MatterSupport` and hands off via the
  network commissioning window. Keep BLE off in the server config.
- **The server is Alpha**, not certified by the CSA yet. Expected progression to Beta and
  then certification during the alpha/beta phase. Treat protocol stability as best-effort;
  pin versions and test before upgrading.

**Plugin scoping implication:** Milestones M4 and M5 (Wi-Fi OnOff and color dimmer) are
unaffected and proceed as planned. Milestone M6 (sensors) and beyond likely need
re-scoping — either to Wi-Fi sensor alternatives (Aqara P3 series and similar Wi-Fi Matter
sensors), or deferred until Thread is fixed in matter-server, or as a contingency the
plugin's WebSocket client falls back to python-matter-server for Thread devices
specifically (WebSocket API is drop-in compatible).

## 3. Indigo Plugin Scaffold

The plugin follows the standard Indigo plugin structure:

```
indigo-matter.indigoPlugin/
├── Contents/
│   ├── Info.plist
│   ├── Resources/
│   │   ├── Actions.xml
│   │   ├── Devices.xml
│   │   ├── Events.xml             # (empty for v1)
│   │   ├── MenuItems.xml
│   │   ├── PluginConfig.xml
│   │   └── plugin.py
│   └── Server Plugin/
```

Use `indigo-domio-plugin` as the reference style guide for module structure, logging conventions, and error handling. Anything not specified here should follow that plugin's conventions.

### 3.1 `Info.plist` essentials

- `CFBundleIdentifier`: `com.simon.indigo-matter`
- `PluginVersion`: semver (start `1.0.0`)
- `ServerApiVersion`: match Indigo 2024.1+ (verify against target Indigo version)
- `IwsApiVersion`: as required by Indigo's HTTP plugin API

### 3.2 `Devices.xml` skeleton

The full device type catalog for v1 cluster scope:

```xml
<?xml version="1.0"?>
<Devices>
    <!-- Relay: OnOff only -->
    <Device id="matterRelay" type="relay">
        <Name>Matter Relay</Name>
        <ConfigUI>
            <Field id="nodeId" type="textfield" readonly="yes">
                <Label>Matter Node ID:</Label>
            </Field>
            <Field id="endpointId" type="textfield" readonly="yes">
                <Label>Endpoint:</Label>
            </Field>
            <Field id="vendorName" type="textfield" readonly="yes">
                <Label>Vendor:</Label>
            </Field>
            <Field id="productName" type="textfield" readonly="yes">
                <Label>Product:</Label>
            </Field>
        </ConfigUI>
        <States>
            <State id="onOffState">
                <ValueType boolType="OnOff">Boolean</ValueType>
                <TriggerLabel>On/Off State</TriggerLabel>
                <ControlPageLabel>On/Off State</ControlPageLabel>
            </State>
        </States>
        <UiDisplayStateId>onOffState</UiDisplayStateId>
    </Device>

    <!-- Dimmer: OnOff + LevelControl -->
    <Device id="matterDimmer" type="dimmer">
        <Name>Matter Dimmer</Name>
        <ConfigUI>
            <!-- same readonly identity fields as relay -->
            <Field id="nodeId" type="textfield" readonly="yes">
                <Label>Matter Node ID:</Label>
            </Field>
            <Field id="endpointId" type="textfield" readonly="yes">
                <Label>Endpoint:</Label>
            </Field>
            <Field id="vendorName" type="textfield" readonly="yes">
                <Label>Vendor:</Label>
            </Field>
            <Field id="productName" type="textfield" readonly="yes">
                <Label>Product:</Label>
            </Field>
        </ConfigUI>
        <!-- States inherited from dimmer base type: onOffState, brightnessLevel -->
    </Device>

    <!-- Color dimmer: OnOff + LevelControl + ColorControl -->
    <Device id="matterColorDimmer" type="dimmer">
        <Name>Matter Color Dimmer</Name>
        <SupportsColor>true</SupportsColor>
        <SupportsRGB>true</SupportsRGB>
        <SupportsWhite>true</SupportsWhite>
        <SupportsWhiteTemperature>true</SupportsWhiteTemperature>
        <SupportsRGBandWhiteSimultaneously>false</SupportsRGBandWhiteSimultaneously>
        <WhiteTemperatureMin>2000</WhiteTemperatureMin>
        <WhiteTemperatureMax>6500</WhiteTemperatureMax>
        <ConfigUI>
            <!-- identity fields as above -->
        </ConfigUI>
    </Device>

    <!-- Sensors -->
    <Device id="matterTemperatureSensor" type="sensor">
        <Name>Matter Temperature Sensor</Name>
        <ConfigUI>
            <!-- identity fields -->
        </ConfigUI>
        <States>
            <State id="sensorValue">
                <ValueType>Number</ValueType>
                <TriggerLabel>Temperature</TriggerLabel>
                <TriggerLabelPrefix>Temperature is</TriggerLabelPrefix>
                <ControlPageLabel>Temperature</ControlPageLabel>
            </State>
            <State id="batteryLevel">
                <ValueType>Integer</ValueType>
                <TriggerLabel>Battery Level</TriggerLabel>
                <ControlPageLabel>Battery Level</ControlPageLabel>
            </State>
        </States>
        <UiDisplayStateId>sensorValue</UiDisplayStateId>
    </Device>

    <Device id="matterHumiditySensor" type="sensor">
        <Name>Matter Humidity Sensor</Name>
        <ConfigUI>
            <!-- identity fields -->
        </ConfigUI>
        <States>
            <State id="sensorValue">
                <ValueType>Number</ValueType>
                <TriggerLabel>Humidity</TriggerLabel>
                <TriggerLabelPrefix>Humidity is</TriggerLabelPrefix>
                <ControlPageLabel>Humidity</ControlPageLabel>
            </State>
        </States>
        <UiDisplayStateId>sensorValue</UiDisplayStateId>
    </Device>

    <Device id="matterMotionSensor" type="sensor">
        <Name>Matter Motion Sensor</Name>
        <ConfigUI>
            <!-- identity fields -->
        </ConfigUI>
        <States>
            <State id="onOffState">
                <ValueType boolType="OnOff">Boolean</ValueType>
                <TriggerLabel>Motion</TriggerLabel>
                <ControlPageLabel>Motion</ControlPageLabel>
            </State>
        </States>
        <UiDisplayStateId>onOffState</UiDisplayStateId>
    </Device>

    <Device id="matterContactSensor" type="sensor">
        <Name>Matter Contact Sensor</Name>
        <ConfigUI>
            <!-- identity fields -->
        </ConfigUI>
        <States>
            <State id="onOffState">
                <ValueType boolType="OpenClosed">Boolean</ValueType>
                <TriggerLabel>Contact</TriggerLabel>
                <ControlPageLabel>Contact</ControlPageLabel>
            </State>
        </States>
        <UiDisplayStateId>onOffState</UiDisplayStateId>
    </Device>

    <Device id="matterIlluminanceSensor" type="sensor">
        <Name>Matter Illuminance Sensor</Name>
        <ConfigUI>
            <!-- identity fields -->
        </ConfigUI>
        <States>
            <State id="sensorValue">
                <ValueType>Number</ValueType>
                <TriggerLabel>Illuminance</TriggerLabel>
                <TriggerLabelPrefix>Illuminance is</TriggerLabelPrefix>
                <ControlPageLabel>Illuminance (lux)</ControlPageLabel>
            </State>
        </States>
        <UiDisplayStateId>sensorValue</UiDisplayStateId>
    </Device>

    <!-- Thermostat -->
    <Device id="matterThermostat" type="thermostat">
        <Name>Matter Thermostat</Name>
        <ConfigUI>
            <!-- identity fields -->
        </ConfigUI>
        <!-- States inherited from thermostat base: hvacMode, hvacFanMode,
             temperatureInputs, setpoints, etc. -->
    </Device>

    <!-- Unknown / unsupported Matter device -->
    <Device id="matterUnknown" type="custom">
        <Name>Matter Device (unsupported clusters)</Name>
        <ConfigUI>
            <!-- identity fields + clusters list as readonly -->
            <Field id="supportedClusters" type="textfield" readonly="yes">
                <Label>Supported Clusters:</Label>
            </Field>
        </ConfigUI>
        <States>
            <State id="reachable">
                <ValueType boolType="YesNo">Boolean</ValueType>
                <TriggerLabel>Reachable</TriggerLabel>
                <ControlPageLabel>Reachable</ControlPageLabel>
            </State>
        </States>
    </Device>
</Devices>
```

### 3.3 `PluginConfig.xml`

```xml
<?xml version="1.0"?>
<PluginConfig>
    <Field id="matterServerHost" type="textfield" defaultValue="localhost">
        <Label>matterjs-server host:</Label>
    </Field>
    <Field id="matterServerPort" type="textfield" defaultValue="5580">
        <Label>matterjs-server port:</Label>
    </Field>
    <Field id="matterServerPath" type="textfield" defaultValue="/ws">
        <Label>matterjs-server WebSocket path:</Label>
    </Field>
    <Field id="storagePath" type="textfield"
           defaultValue="~/Library/Application Support/com.simon.indigo-matter/matter-server">
        <Label>matter-server storage path:</Label>
    </Field>
    <!-- No plugin HTTP port: the Domio API is served by the Indigo Web Server
         (IWS) as hidden-action handlers (see §4), not a standalone server. -->
    <Field id="manageLaunchAgent" type="checkbox" defaultValue="true">
        <Label>Manage matterjs-server LaunchAgent automatically:</Label>
    </Field>
    <Field id="verboseLogging" type="checkbox" defaultValue="false">
        <Label>Verbose Matter logging:</Label>
    </Field>
    <Field id="traceWebSocket" type="checkbox" defaultValue="false">
        <Label>Trace matterjs-server WebSocket messages (debug):</Label>
    </Field>
</PluginConfig>
```

There is no plugin HTTP port: the Domio API is served by the Indigo Web Server (IWS) as
hidden-action handlers (see §4), so no standalone server or port configuration is needed.

### 3.4 `MenuItems.xml`

```xml
<?xml version="1.0"?>
<MenuItems>
    <MenuItem id="restartMatterServer">
        <Name>Restart matterjs-server</Name>
        <CallbackMethod>menuRestartMatterServer</CallbackMethod>
    </MenuItem>
    <MenuItem id="showMatterServerLogs">
        <Name>Open matterjs-server log…</Name>
        <CallbackMethod>menuShowMatterServerLogs</CallbackMethod>
    </MenuItem>
    <MenuItem id="commissionDeviceManually">
        <Name>Commission device by setup code (advanced)…</Name>
        <CallbackMethod>menuCommissionDeviceManually</CallbackMethod>
        <ConfigUI>
            <Field id="setupCode" type="textfield">
                <Label>Setup code (MT:... or 11 digits):</Label>
            </Field>
            <Field id="suggestedName" type="textfield">
                <Label>Name:</Label>
            </Field>
        </ConfigUI>
    </MenuItem>
    <MenuItem id="exportFabricBackup">
        <Name>Export fabric backup…</Name>
        <CallbackMethod>menuExportFabricBackup</CallbackMethod>
    </MenuItem>
</MenuItems>
```

## 4. HTTP API — Indigo Web Server (IWS) handlers

The plugin does **not** run its own HTTP server. (`aiohttp` is **not** present in the
Indigo framework Python on the target machines, and IWS is the idiomatic mechanism.) The
Domio API (`API.md` v1.1) is served as **hidden-action handlers** on the Indigo Web
Server, exactly like `indigo-domio-plugin`'s device-history/status/pages handlers and the
SDK "HTTP Responder" example.

**Registration** — one `<Action … uiPath="hidden">` per handler in `Actions.xml`:

```xml
<Action id="status" uiPath="hidden">
    <Name>matter status</Name>
    <CallbackMethod>http_status</CallbackMethod>
</Action>
<!-- commission, commission status (jobId path arg), decommission, diagnostics … -->
```

Each handler is a method on the `Plugin` class with the IWS signature, returning an
`indigo.Dict` with `status` / `headers` / `content`:

```python
def http_commission(self, action, dev=None, caller_waiting_for_result=None):
    props = dict(action.props)
    method = props.get("incoming_request_method", "GET")        # "GET" | "POST"
    path_args = list(props.get("file_path", []))                # trailing /{jobId} etc.
    query = dict(props.get("url_query_args", {}))               # request params
    # ... bridge into the loop: self.runtime.submit(coro).result(timeout) ...
    reply = indigo.Dict()
    reply["status"] = 202
    reply["headers"] = indigo.Dict({"Content-Type": "application/json"})
    reply["content"] = json.dumps({"jobId": job_id, "estimatedDurationSeconds": 30})
    return reply
```

**URL shape:** `…/message/com.simon.indigo-matter/{handler}[/{pathArg}]`, reachable locally
on `:8176` and remotely via the Reflector (`https://{reflector}.indigodomo.net/message/…`)
with the same `Authorization: Bearer {key}` Domio already uses.

**Authentication:** enforced by Indigo/Reflector **before** the handler runs — the plugin
does not validate the credential itself. (Confirmed: `domio-code`'s `DeviceHistoryService`
and `IndigoRESTService` already call plugin handlers this way over the Reflector.)

**Request input:** parameters arrive as `url_query_args` (and `body_params` if a handler
chooses form-encoded POST); the request method is `incoming_request_method`; positional
path components after the handler name are in `file_path`.

The handlers are thin transport adapters — they bridge into the asyncio loop via
`self.runtime.submit(coro).result(timeout)` and delegate all logic to `commission_jobs`
and `device_sync`. There is no standalone server, no port to configure, and no
`auth_middleware`.

## 5. Worked Example: OnOff Cluster Handler

Concrete implementation of the `ClusterHandler` ABC for the OnOff cluster. Every other v1 cluster handler should follow this pattern.

```python
# matter_handlers/on_off.py

import indigo
from typing import Optional
from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand


class OnOffHandler(ClusterHandler):
    """Maps the Matter OnOff cluster (0x0006) to an Indigo Relay device.

    If LevelControl is also present on the same endpoint, the DimmerHandler
    is preferred and this handler skips device creation (it still handles
    OnOff attribute updates and commands as part of the Dimmer state).
    """

    cluster_id = 0x0006
    cluster_name = "OnOff"

    # Attribute IDs on the OnOff cluster (Matter spec)
    ATTR_ON_OFF = 0x0000

    # Command IDs on the OnOff cluster
    CMD_OFF = "Off"
    CMD_ON = "On"
    CMD_TOGGLE = "Toggle"

    def is_primary_for(self, node, endpoint) -> bool:
        """Return True only if no other handler (e.g. LevelControl) will own
        this endpoint. Composition order: Color > Dimmer > Relay."""
        cluster_ids = {c.cluster_id for c in endpoint.clusters}
        if 0x0008 in cluster_ids:  # LevelControl present → Dimmer handles it
            return False
        return True

    def create_indigo_devices(self, node, endpoint) -> list[IndigoDeviceSpec]:
        if not self.is_primary_for(node, endpoint):
            return []
        return [
            IndigoDeviceSpec(
                device_type_id="matterRelay",
                name=f"{node.suggested_name}",
                props={
                    "nodeId": str(node.node_id),
                    "endpointId": str(endpoint.endpoint_id),
                    "vendorName": node.vendor_name,
                    "productName": node.product_name,
                },
                initial_states={"onOffState": False},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_ON_OFF]

    def on_attribute_update(self, indigo_dev, attribute_id, value) -> dict:
        if attribute_id == self.ATTR_ON_OFF:
            # value is a bool from matterjs-server
            return {"onOffState": bool(value)}
        return {}

    def handle_indigo_action(self, indigo_dev, action) -> Optional[MatterCommand]:
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=self.cluster_id, command=self.CMD_ON, args={},
            )
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=self.cluster_id, command=self.CMD_OFF, args={},
            )
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=self.cluster_id, command=self.CMD_TOGGLE, args={},
            )
        return None
```

And the supporting base class:

```python
# matter_handlers/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IndigoDeviceSpec:
    """Description of an Indigo device to create for a Matter endpoint."""
    device_type_id: str
    name: str
    props: dict
    initial_states: dict = field(default_factory=dict)


@dataclass
class MatterCommand:
    """A Matter cluster command to send via matterjs-server."""
    node_id: int
    endpoint: int
    cluster: int
    command: str
    args: dict


class ClusterHandler(ABC):
    cluster_id: int
    cluster_name: str

    @abstractmethod
    def is_primary_for(self, node, endpoint) -> bool: ...

    @abstractmethod
    def create_indigo_devices(self, node, endpoint) -> list[IndigoDeviceSpec]: ...

    @abstractmethod
    def attributes_to_subscribe(self) -> list[int]: ...

    @abstractmethod
    def on_attribute_update(self, indigo_dev, attribute_id, value) -> dict: ...

    @abstractmethod
    def handle_indigo_action(self, indigo_dev, action) -> Optional[MatterCommand]: ...
```

Composition note: a single endpoint may have multiple primary handlers (rare) or one primary and multiple supplementary. The plugin's commissioning logic walks handlers in priority order (Color > Dimmer > Relay > sensors > thermostat), calls `create_indigo_devices` on each, and merges the results.

## 6. Test Devices for Milestones

Hardware to acquire ahead of each milestone. Models chosen for Matter certification + UK availability + reasonable cost (~£15–£60 each).

| Milestone | Capability tested | Suggested device | Cluster scope |
|---|---|---|---|
| M4 | OnOff Wi-Fi | TP-Link Tapo P125M smart plug (Matter over Wi-Fi) | OnOff |
| M5 | Dimmer + color | Nanoleaf Essentials A19 Matter bulb | OnOff, LevelControl, ColorControl |
| M6 (motion) | Thread occupancy | Aqara Motion and Light Sensor P2 | OccupancySensing, IlluminanceMeasurement |
| M6 (temp/humidity) | Thread temp+humidity | Aqara Temperature and Humidity Sensor T1 (Matter over Thread variant) | TemperatureMeasurement, RelativeHumidityMeasurement |
| M6 (contact) | Thread contact | Eve Door & Window | BooleanState |
| M7 | Thermostat | Bosch Thermostat II BTH-RM230Z (verify Matter support before purchase) or any Matter-certified TRV | Thermostat, FanControl |

Verify Matter certification status on the CSA database (https://csa-iot.org/csa-iot_products/) before purchase — Matter support is sometimes advertised before being shipped in firmware.

For M4 specifically, the Tapo P125M is the safe choice: low cost, Wi-Fi (no Thread needed yet), and well-tested with python-matter-server / matterjs-server.

## 7. Coding Conventions

Follow `indigo-domio-plugin` conventions for:
- Logging (use Indigo's `self.logger`, levels: error/warning/info/debug).
- Async patterns (asyncio with explicit task management; never `asyncio.run` from within Indigo's main thread).
- Error handling (specific exception types, structured error responses, no bare excepts).
- Type hints throughout (Python 3.11+).
- Module structure (one cluster handler per file under `matter_handlers/`).

Tests: pytest, with matterjs-server mocked at the WebSocket layer. A real-device integration test suite is out of scope for v1.
