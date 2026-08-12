"""M-issue-3: PowerSource cluster (0x002F) — battery level for sensor devices.

Tests cover:
- BatPercentRemaining half-percent → 0-100 transform
- Clamping of out-of-spec values
- None value returns no states
- Missing-state guard (devices without batteryLevel stay quiet)
- Node-scoped fan-out to multiple devices across a node
- Fan-out respects the _active gate
- State priming across endpoints (PowerSource on ep0, sensor on ep1)
- SupportsBatteryLevel prop set when PowerSource present, absent otherwise
- resolve_power_coverage: EndpointList (0x001F) as the authority, with the
  pre-#205 heuristic as a per-source fallback (issue #205)
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from matter_handlers.power_source import (
    ATTR_BAT_PERCENT_REMAINING,
    ATTR_ENDPOINT_LIST,
    CLUSTER_POWER_SOURCE,
    PowerSourceHandler,
    resolve_power_coverage,
)
from matter_handlers.registry import HandlerRegistry


# ---------------------------------------------------------------------------
# Handler unit tests
# ---------------------------------------------------------------------------

class _Dev:
    """Minimal Indigo device stub for handler tests."""
    def __init__(self, states: dict | None = None):
        self.states = states if states is not None else {"batteryLevel": 0}


def test_transform_half_percent():
    h = PowerSourceHandler()
    # 200 → 100 %, 100 → 50 %, 1 → 0 (floor of 0.5)
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, 200) == {"batteryLevel": 100}
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, 100) == {"batteryLevel": 50}
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, 1) == {"batteryLevel": 0}
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, 60) == {"batteryLevel": 30}


def test_transform_clamp_low():
    h = PowerSourceHandler()
    # value 0 → 0 (no negative)
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, 0) == {"batteryLevel": 0}


def test_transform_clamp_high():
    h = PowerSourceHandler()
    # out-of-spec value above 200 → clamped to 100
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, 255) == {"batteryLevel": 100}


def test_none_value_returns_empty():
    h = PowerSourceHandler()
    assert h.on_attribute_update(_Dev(), ATTR_BAT_PERCENT_REMAINING, None) == {}


def test_wrong_attribute_returns_empty():
    h = PowerSourceHandler()
    assert h.on_attribute_update(_Dev(), 0x0000, 100) == {}  # Status attr, not BatPercentRemaining


def test_missing_state_guard():
    # A device created before this feature lacks the batteryLevel state;
    # update must return {} so the device isn't errored with an unknown state key.
    h = PowerSourceHandler()
    dev_without_battery = _Dev(states={"onOffState": False})
    assert h.on_attribute_update(dev_without_battery, ATTR_BAT_PERCENT_REMAINING, 150) == {}


def test_is_non_primary():
    h = PowerSourceHandler()
    assert h.is_primary_for(None, None) is False


def test_creates_no_devices():
    h = PowerSourceHandler()
    assert h.create_indigo_devices(None, None) == []


def test_subscribes_to_bat_percent():
    h = PowerSourceHandler()
    assert ATTR_BAT_PERCENT_REMAINING in h.attributes_to_subscribe()


def test_subscribes_to_endpoint_list():
    """Documentation, not plumbing (start_listening streams everything) — but
    the list is where a reader looks to find out what the handler consumes."""
    h = PowerSourceHandler()
    assert ATTR_ENDPOINT_LIST in h.attributes_to_subscribe()


def test_live_endpoint_list_event_is_a_noop():
    """Topology, not a reading: there is no Indigo state to write, and the
    node_updated that accompanies it rebuilds coverage from the whole snapshot."""
    h = PowerSourceHandler()
    assert h.on_attribute_update(_Dev(), ATTR_ENDPOINT_LIST, [1, 2]) == {}


def test_handle_action_returns_none():
    h = PowerSourceHandler()
    assert h.handle_indigo_action(None, object()) is None


def test_node_scoped_flag():
    h = PowerSourceHandler()
    assert h.node_scoped is True


def test_registered_in_default_handlers():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_POWER_SOURCE)
    assert handler is not None
    assert isinstance(handler, PowerSourceHandler)


# ---------------------------------------------------------------------------
# resolve_power_coverage — issue #205
#
# EndpointList (0x001F, core §11.7.7.32) is the authority; the pre-#205
# "more than one source ⇒ confine each to its own endpoint" heuristic survives
# only as a PER-SOURCE fallback for rev-1 firmware that predates the attribute.
# ---------------------------------------------------------------------------

_NODE_EPS = (0, 1, 2)


def test_empty_endpoint_list_powers_the_whole_node():
    """The spec's own words: an empty list means the entire node."""
    cov = resolve_power_coverage([0], {0: []}, _NODE_EPS)
    assert cov.by_source == {0: frozenset({0, 1, 2})}
    assert cov.covered == frozenset({0, 1, 2})
    assert cov.from_endpoint_list == frozenset({0})


def test_non_empty_endpoint_list_powers_exactly_those_endpoints():
    """A source on ep0 listing [1] powers ep1 — and NOT ep2, which is the
    answer the old heuristic could never give (one source ⇒ node-wide)."""
    cov = resolve_power_coverage([0], {0: [1]}, _NODE_EPS)
    assert cov.by_source == {0: frozenset({0, 1})}
    assert cov.covered == frozenset({0, 1})
    assert 2 not in cov.covered


def test_conformant_list_including_its_own_endpoint_is_taken_as_written():
    cov = resolve_power_coverage([1], {1: [1, 2]}, _NODE_EPS)
    assert cov.by_source == {1: frozenset({1, 2})}


def test_a_list_omitting_its_own_endpoint_still_gets_it():
    """The spec says the list SHALL include the source's own endpoint, so
    adding it is a no-op for conformant firmware and a repair for the rest."""
    cov = resolve_power_coverage([2], {2: [1]}, _NODE_EPS)
    assert cov.by_source == {2: frozenset({1, 2})}


def test_absent_list_with_one_source_falls_back_to_node_wide():
    """Rev-1 firmware, single source — the FP300 shape, unchanged by #205."""
    cov = resolve_power_coverage([0], {0: None}, _NODE_EPS)
    assert cov.by_source == {0: frozenset({0, 1, 2})}
    assert cov.from_endpoint_list == frozenset()


def test_absent_lists_with_two_sources_confine_each_to_its_own_endpoint():
    """Rev-1 bridge — the issue #82 posture, kept as the fallback."""
    cov = resolve_power_coverage([1, 2], {}, _NODE_EPS)
    assert cov.by_source == {1: frozenset({1}), 2: frozenset({2})}
    assert cov.covered == frozenset({1, 2})
    assert cov.from_endpoint_list == frozenset()


def test_a_mixed_bridge_believes_the_child_that_answered():
    """The reason the fallback is per SOURCE and not per node: the spec's
    legacy note names bridges, so a mixed rev-1/rev-2 node is the likely one.
    ep1 reported its list and is taken at its word; ep2 said nothing and gets
    the heuristic — a node-level all-or-nothing would discard ep1's evidence."""
    cov = resolve_power_coverage([1, 2], {1: [1, 3], 2: None}, (0, 1, 2, 3))
    assert cov.by_source == {1: frozenset({1, 3}), 2: frozenset({2})}
    assert cov.from_endpoint_list == frozenset({1})


@pytest.mark.parametrize("raw", [
    ["x"],              # a list of something that is not an endpoint number
    {"0": 1},           # not a list at all (a tag-based struct)
    [1, None],          # a None element mid-list
])
def test_a_malformed_endpoint_list_degrades_to_the_fallback(raw):
    """Degrade, don't raise — the same idiom as _resolve_meter_target: one
    non-conformant device must not abort a whole node's battery routing."""
    noted = []
    cov = resolve_power_coverage([0], {0: raw}, _NODE_EPS,
                                 note=lambda *args: noted.append(args))
    assert cov.by_source == {0: frozenset({0, 1, 2})}   # single source ⇒ node-wide
    assert cov.from_endpoint_list == frozenset()
    assert noted, "an unusable EndpointList must be reported at debug"


def test_an_absent_endpoint_list_is_not_noted():
    """Rev-1 firmware is expected, not anomalous — only an unusable VALUE is
    worth a log line."""
    noted = []
    resolve_power_coverage([0], {0: None}, _NODE_EPS, note=lambda *args: noted.append(args))
    assert not noted


def test_a_malformed_endpoint_list_without_a_note_callable_does_not_raise():
    cov = resolve_power_coverage([0], {0: "nonsense"}, _NODE_EPS)
    assert cov.by_source == {0: frozenset({0, 1, 2})}


def test_no_power_sources_covers_nothing():
    cov = resolve_power_coverage([], {}, _NODE_EPS)
    assert cov.by_source == {}
    assert cov.covered == frozenset()
    assert cov.from_endpoint_list == frozenset()


# ---------------------------------------------------------------------------
# device_sync integration tests
# (reuse helpers from test_device_sync.py via conftest + local FakeDev copies)
# ---------------------------------------------------------------------------

# Import fakes from test_device_sync (they're not importable as a module but we
# can replicate the minimal subset we need here, or import the symbols directly).
# Since pytest adds tests/ to sys.path via conftest, import directly.
from test_device_sync import FakeDev, FakeDeviceFactory, FakeDevices, FakeFolderFactory  # noqa: E402


# A sensor node with PowerSource on endpoint 0 and a temperature sensor on endpoint 1.
# "0/47/12" = endpoint 0, cluster 0x002F (PowerSource), attr 0x000C (BatPercentRemaining)
BATTERY_SENSOR_NODE = {
    "node_id": 200,
    "attributes": {
        "0/47/12": 150,         # BatPercentRemaining = 150 → 75 %
        "1/1026/0": 2185,       # TemperatureMeasurement MeasuredValue = 21.85 °C
        "1/29/0": [{"0": 770, "1": 1}],
    },
}

# A two-sensor node: contact on ep1, humidity on ep2, PowerSource on ep0.
MULTI_SENSOR_NODE = {
    "node_id": 201,
    "attributes": {
        "0/47/12": 100,         # BatPercentRemaining = 100 → 50 %
        "1/69/0": True,         # BooleanState (contact) ep1
        "1/29/0": [{"0": 21, "1": 1}],
        "2/1029/0": 4500,       # RelativeHumidity ep2
        "2/29/0": [{"0": 775, "1": 1}],
    },
}


@pytest.fixture
def indigo_env(mock_indigo_base):
    indigo = mock_indigo_base
    devices = FakeDevices()
    indigo.devices = devices
    indigo.devices.folder = FakeFolderFactory(devices)
    indigo.device = FakeDeviceFactory(devices)
    indigo.kProtocol = SimpleNamespace(Plugin="plugin")
    return indigo, devices


@pytest.fixture
def ds(indigo_env, mock_logger):
    import device_sync
    importlib.reload(device_sync)
    return device_sync.DeviceSync(HandlerRegistry(), mock_logger)


def test_supports_battery_level_prop_set_when_power_source_present(ds, indigo_env):
    """SupportsBatteryLevel prop must be True when PowerSource is on the node."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(BATTERY_SENSOR_NODE, "Temp Sensor")
    dev = devices[result["primaryDeviceId"]]
    assert dev.pluginProps.get("SupportsBatteryLevel") is True


def test_supports_battery_level_prop_absent_when_no_power_source(ds, indigo_env):
    """SupportsBatteryLevel must NOT be set on nodes without PowerSource."""
    _indigo, devices = indigo_env
    node = {"node_id": 210, "attributes": {"1/1026/0": 2000, "1/29/0": [{"0": 770}]}}
    result = ds.create_from_raw(node, "Temp No Battery")
    dev = devices[result["primaryDeviceId"]]
    assert "SupportsBatteryLevel" not in dev.pluginProps


def test_prime_states_includes_battery_from_other_endpoint(ds, indigo_env):
    """On creation the sensor is primed with battery level from PowerSource on ep0."""
    _indigo, devices = indigo_env
    # Give the device a batteryLevel state so the guard passes
    original_create = _indigo.device.create

    def create_with_battery(**kw):
        dev = original_create(**kw)
        dev.states["batteryLevel"] = 0  # simulate Devices.xml providing the state
        return dev

    _indigo.device.create = create_with_battery
    result = ds.create_from_raw(BATTERY_SENSOR_NODE, "Temp Sensor")
    dev = devices[result["primaryDeviceId"]]
    assert dev.states.get("batteryLevel") == 75   # 150 // 2


def test_node_scoped_fanout_updates_all_node_devices(ds, indigo_env):
    """A PowerSource attribute_updated event fans out to all devices on the node."""
    import protocol
    from protocol import MatterEvent

    _indigo, devices = indigo_env

    # Create two sensor devices on node 201
    original_create = _indigo.device.create

    def create_with_battery(**kw):
        dev = original_create(**kw)
        dev.states["batteryLevel"] = 0
        return dev

    _indigo.device.create = create_with_battery
    ds.create_from_raw(MULTI_SENSOR_NODE, "Multi Sensor")
    id1 = ds.lookup(201, 1)   # contact sensor
    id2 = ds.lookup(201, 2)   # humidity sensor
    assert id1 is not None and id2 is not None and id1 != id2

    # Fire a battery update on endpoint 0 (PowerSource lives there)
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=201, endpoint=0, cluster=0x002F, attribute=0x000C, value=80,
    ))
    # Both devices should receive batteryLevel = 80 // 2 = 40
    assert devices[id1].states.get("batteryLevel") == 40
    assert devices[id2].states.get("batteryLevel") == 40


def test_node_scoped_fanout_respects_active_gate(ds, indigo_env):
    """Fan-out skips inactive devices when the active gate is engaged."""
    import protocol
    from protocol import MatterEvent

    _indigo, devices = indigo_env

    original_create = _indigo.device.create

    def create_with_battery(**kw):
        dev = original_create(**kw)
        dev.states["batteryLevel"] = 0
        return dev

    _indigo.device.create = create_with_battery
    ds.create_from_raw(MULTI_SENSOR_NODE, "Multi Sensor")
    id1 = ds.lookup(201, 1)
    id2 = ds.lookup(201, 2)

    # Both devices were primed with the initial battery value (100 → 50 %)
    # on creation. Record that baseline before activating the gate.
    id2_before = devices[id2].states.get("batteryLevel")

    # Mark only id1 as active; id2 is gated out
    ds.set_active(id1, True)

    # Send a *different* battery value so we can tell if id2 received the update
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=201, endpoint=0, cluster=0x002F, attribute=0x000C, value=60,
    ))
    assert devices[id1].states.get("batteryLevel") == 30   # updated (60 // 2)
    assert devices[id2].states.get("batteryLevel") == id2_before   # gated: still at primed value


def test_node_scoped_event_on_missing_endpoint_does_not_crash(ds, indigo_env):
    """A PowerSource event for a node where no devices are indexed yet is silently ignored."""
    import protocol
    from protocol import MatterEvent

    # No devices created — index is empty. This must not raise.
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=999, endpoint=0, cluster=0x002F, attribute=0x000C, value=100,
    ))


def test_node_scoped_fanout_survives_deleted_device(ds, indigo_env):
    """If one device is deleted from indigo.devices between the index snapshot and the
    fan-out loop iteration, the KeyError must NOT abort the remaining devices on the node.
    The surviving device must still receive the batteryLevel update."""
    import protocol
    from protocol import MatterEvent

    _indigo, devices = indigo_env

    original_create = _indigo.device.create

    def create_with_battery(**kw):
        dev = original_create(**kw)
        dev.states["batteryLevel"] = 0
        return dev

    _indigo.device.create = create_with_battery
    ds.create_from_raw(MULTI_SENSOR_NODE, "Multi Sensor")
    id1 = ds.lookup(201, 1)   # contact sensor
    id2 = ds.lookup(201, 2)   # humidity sensor
    assert id1 is not None and id2 is not None and id1 != id2

    # Simulate id1 being deleted from indigo.devices (index still holds the stale id).
    del devices._by_id[id1]

    # Fire a battery update — id1 lookup raises KeyError; id2 must still be updated.
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=201, endpoint=0, cluster=0x002F, attribute=0x000C, value=80,
    ))
    # id2 should have been updated despite id1 causing an error
    assert devices[id2].states.get("batteryLevel") == 40   # 80 // 2
