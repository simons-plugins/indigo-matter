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
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from matter_handlers.power_source import (
    ATTR_BAT_PERCENT_REMAINING,
    CLUSTER_POWER_SOURCE,
    PowerSourceHandler,
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
