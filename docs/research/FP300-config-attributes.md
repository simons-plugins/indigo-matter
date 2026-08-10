# Aqara FP300 — exposed config attributes over Matter

**Unit:** Presence Multi-Sensor FP300, vendor `0x115F` (4447), product `0x2005` (8197)
**Firmware:** software `1.1.3.8`, hardware `1.0.0.0`, Matter `1.4.1`
**Fabric node id:** **56** (`0x38`) — the live node
**Discovered:** 2026-08-10T20:06Z — read-only `get_node`, no writes
**Raw dumps:**
- [`fp300-node56-attributes-20260810.json`](./fp300-node56-attributes-20260810.json) — **authoritative**, the live unit on the current matter-server
- [`fp300-node43-attributes-20260810.json`](./fp300-node43-attributes-20260810.json) — the same physical unit read via a since-retired duplicate matter-server (issue #182)

The two dumps agree on **every** attribute below — endpoint layout, both cluster
revisions, both FeatureMaps, `AttributeList`, `HoldTimeLimits`, and the absence of any
manufacturer-specific cluster — so these findings are confirmed independently.

Re-run the dump after any firmware update and diff it against the raw file. Aqara has
already shipped at least two firmware revisions to this unit: stale records for the
same device (nodes 45 and 55, from repeated re-commissioning) still report `1.1.0.1`,
and the attribute surface below is `1.1.3.8`.

## Endpoint layout

| Endpoint | Device type | Clusters (ServerList) |
|---|---|---|
| 0 | Root Node (0x0012), Otherwise-Ep (0x0016) | Descriptor, Binding, BasicInformation, OTA, GeneralCommissioning, NetworkCommissioning, GeneralDiagnostics, ThreadNetworkDiagnostics, … |
| **1** | **Occupancy Sensor (0x0107)** | Identify, Descriptor, **BooleanStateConfiguration (0x0080)**, **OccupancySensing (0x0406)** |
| 2 | Light Sensor (0x0106) | Identify, Descriptor, IlluminanceMeasurement (0x0400) |
| 3 | Temperature Sensor (0x0302) | Identify, Descriptor, TemperatureMeasurement (0x0402) |
| 4 | Humidity Sensor (0x0307) | Identify, Descriptor, RelativeHumidityMeasurement (0x0405) |
| 5 | Power Source (0x0011) | Descriptor, PowerSource (0x002F) |

Both tunables live on **endpoint 1**.

## The tunables

| Attribute | Cluster | Current | Writable? | Valid range |
|---|---|---|---|---|
| **HoldTime** `0x0003` | OccupancySensing `0x0406` | `10` (seconds) | Yes — `HoldTimeLimits` present, so the spec's optional write is implemented | **1 – 300 s**, default 10 |
| **CurrentSensitivityLevel** `0x0000` | BooleanStateConfiguration `0x0080` | `1` | Yes (`RW VO`) | **0 – 2** (3 levels), default 1 |

### Read-only context attributes

| Attribute | Cluster | Value | Meaning |
|---|---|---|---|
| `HoldTimeLimits` `0x0004` | 0x0406 | `{min: 1, max: 300, default: 10}` | Bounds for HoldTime — validate against this before writing |
| `Occupancy` `0x0000` | 0x0406 | `0` | bitmap8, bit0 = occupied |
| `OccupancySensorType` `0x0001` | 0x0406 | `0` (PIR) | Deprecated in 1.4; **contradicts FeatureMap** — see note below |
| `OccupancySensorTypeBitmap` `0x0002` | 0x0406 | `1` (PIR) | Deprecated in 1.4 |
| `FeatureMap` `0xFFFC` | 0x0406 | `32` = bit5 **RAD** (radar) | Authoritative capability declaration |
| `ClusterRevision` `0xFFFD` | 0x0406 | `5` | Matter 1.4 revision |
| `SupportedSensitivityLevels` `0x0001` | 0x0080 | `3` | Valid indices are 0..2 |
| `DefaultSensitivityLevel` `0x0002` | 0x0080 | `1` | |
| `FeatureMap` `0xFFFC` | 0x0080 | `8` = bit3 **SENSLVL** | Sensitivity-level feature supported |
| `ClusterRevision` `0xFFFD` | 0x0080 | `1` | |

## Findings that change the plan

1. **No manufacturer-specific cluster exists.** Nothing in `0x115FFC00–0x115FFCFF`
   appears in any endpoint's ServerList — in fact no cluster id above `0xFFFF`
   appears anywhere in the node. Sensitivity is *not* vendor-private on this
   firmware; it is the **standard** BooleanStateConfiguration cluster.

2. **This is a Matter 1.4 unit — HoldTime, not the deprecated PIR delays.**
   `AttributeList` for 0x0406 is `[0, 1, 2, 3, 4, 65528, 65529, 65531, 65532, 65533]`.
   `0x0010`/`0x0011` (PIROccupiedToUnoccupiedDelay / PIRUnoccupiedToOccupiedDelay)
   are **absent**. Any code written against the pre-1.4 delay attributes will not
   work here.

3. **`OccupancySensorType` disagrees with the FeatureMap.** The type attributes say
   PIR; the FeatureMap says RAD (radar). The type attributes are deprecated as of
   cluster revision 5, so treat the FeatureMap as authoritative — but don't be
   surprised by tools that report this unit as a PIR.

4. **The cluster exposes no commands and no events** — `AcceptedCommandList`,
   `GeneratedCommandList` and `EventList` are all empty on both clusters. Both
   tunables are plain attribute writes.

## Plugin coverage today

- **Sensitivity is already wired.** `matter_handlers/boolean_state_config.py`
  merges `sensitivityLevel` onto the occupancy device and the "Set Sensitivity
  Level" custom device action writes it (issue #85). Direction (whether 0 is
  low or high) is still behaviourally unverified.
- **HoldTime is not handled at all.** `matter_handlers/sensors.py`'s
  `OccupancyHandler` reads only `Occupancy` (0x0000) from 0x0406. Presence hold
  time is currently invisible to Indigo and unsettable from it.

## Protocol facts verified this session (reusable)

Established live against matter-server 1.2.2 while gathering the above — worth
knowing before building the write path.

- **`read_attribute` works and is not yet wired up.** `protocol.py` defines
  `CMD_READ_ATTR = "read_attribute"` but nothing calls it: `MatterClient` has a
  `write()` wrapper and no `read()`. Args are `{node_id, attribute_path}` with the
  same `"endpoint/cluster/attribute"` decimal form `Protocol.attr_key` already
  builds. **The result is a dict keyed by that path**, not a bare value:
  `{"0/53/7": [...]}`. A read wrapper has to unwrap it.
- **`get_node` returns matter-server's CACHE, not a live read.** Diagnostics
  attributes are not subscribed, so cached values can be hours stale — proven here
  when a sleepy device reported a parent router that no longer existed, while a
  `read_attribute` of the same attribute returned the current value. Use
  `read_attribute` for anything a decision depends on; `get_node` is fine for
  structure (endpoints, cluster lists), which does not drift.
- **uint64 loses precision on the wire.** matter-server serialises 64-bit values as
  JSON numbers, so anything above 2^53 is mangled by any JSON parser using float64
  (`…5720000`-style trailing zeros are the tell). Fine for equality comparison
  between two values from the same source; **not** safe to display or store as an
  identity. No attribute in the table above is affected, but Thread ExtAddress and
  similar are.

## Cautions for the write path

- **A write is device-global, not fabric-local.** This unit is on Apple Home's
  fabric as well as Indigo's, so changing HoldTime or sensitivity from Indigo
  changes the device's behaviour in Apple Home too. Any UI must say so.
- **Aqara firmware can ACK a write and ignore it.** A write is only successful when
  a subsequent read returns the new value; an unverified write must be reported as a
  FAILURE, never as success.
- **Sleepy device: seconds, not milliseconds.** The FP300 is a Thread SED, so a
  read or write can take seconds and can time out — a sibling IKEA sleepy device on
  this mesh polls at roughly 9.5s intervals. The plugin's default 5s
  `COMMAND_TIMEOUT` is too short; retry rather than treating one timeout as failure.
- **Bounds come from the device, never from a constant.** `HoldTimeLimits` (0x0004)
  and `SupportedSensitivityLevels` (0x0001) are the authority, and both are already
  present on this firmware. Reject out-of-range values before sending.

## Node-id history

The physical unit has been commissioned repeatedly, leaving stale records behind.
Only **56** is live; 38/43/45/55 are ghosts (Indigo devices for them should be
deleted). Node id is assigned by the controller at commissioning, so it changes
every time — never hard-code it.
