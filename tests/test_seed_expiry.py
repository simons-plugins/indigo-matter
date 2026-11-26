"""An abandoned dialog-seed record expires (PR #388): if closedDeviceConfigUi never fires
(client crash, dropped connection), the next open must not be stuck ambiguous forever."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from test_device_sync import indigo_env, ds, plugin_cls  # noqa: F401
from test_unread_settings import _stub, _open, _lock

CONNECTED = SimpleNamespace(connected=True)
LIMIT = 600.0


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _setup(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    clock = _Clock()
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED, _seed_clock=clock)
    stub.runtime = MagicMock()
    return dev, stub, clock


def _save(plugin_cls, stub, dev, values):
    stub.runtime.reset_mock()
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    submitted = stub.runtime.submit.call_count
    for call in stub.runtime.submit.call_args_list:
        call.args[0].close()
    return submitted


def test_an_abandoned_slot_older_than_the_limit_is_replaced_and_the_new_dialog_can_save(
        plugin_cls, ds, indigo_env, mock_logger):
    dev, stub, clock = _setup(plugin_cls, ds, indigo_env, mock_logger)
    _open(plugin_cls, stub, dev, "matterLock")                 # never closed
    clock.now += LIMIT + 1
    values = _open(plugin_cls, stub, dev, "matterLock")        # the crashed client's slot is stale
    assert any("unclosed" in str(c) for c in mock_logger.info.call_args_list)
    values["autoRelockTime"] = "90"
    assert _save(plugin_cls, stub, dev, values) == 1


def test_a_young_slot_is_still_marked_ambiguous(plugin_cls, ds, indigo_env, mock_logger):
    dev, stub, clock = _setup(plugin_cls, ds, indigo_env, mock_logger)
    _open(plugin_cls, stub, dev, "matterLock")
    clock.now += LIMIT - 1
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "90"
    assert _save(plugin_cls, stub, dev, values) == 0
    assert not any("unclosed" in str(c) for c in mock_logger.info.call_args_list)


def test_exactly_at_the_limit_the_slot_is_not_yet_old(plugin_cls, ds, indigo_env, mock_logger):
    """"Older than 10 minutes" is strict: at exactly the limit it is still a live dialog."""
    dev, stub, clock = _setup(plugin_cls, ds, indigo_env, mock_logger)
    _open(plugin_cls, stub, dev, "matterLock")
    clock.now += LIMIT
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "90"
    assert _save(plugin_cls, stub, dev, values) == 0


def test_one_second_past_the_limit_replaces(plugin_cls, ds, indigo_env, mock_logger):
    dev, stub, clock = _setup(plugin_cls, ds, indigo_env, mock_logger)
    _open(plugin_cls, stub, dev, "matterLock")
    clock.now += LIMIT + 0.001
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "90"
    assert _save(plugin_cls, stub, dev, values) == 1


def test_one_dialog_left_open_for_an_hour_still_saves(plugin_cls, ds, indigo_env, mock_logger):
    """Expiry is judged when ANOTHER dialog opens, never on save."""
    dev, stub, clock = _setup(plugin_cls, ds, indigo_env, mock_logger)
    values = _open(plugin_cls, stub, dev, "matterLock")
    clock.now += 3600
    values["autoRelockTime"] = "90"
    assert _save(plugin_cls, stub, dev, values) == 1


def test_expiry_is_per_device(plugin_cls, ds, indigo_env, mock_logger):
    import copy
    from test_device_sync import LOCK_SETTINGS_NODE
    _i, devices = indigo_env
    dev, stub, clock = _setup(plugin_cls, ds, indigo_env, mock_logger)
    other = copy.deepcopy(LOCK_SETTINGS_NODE)
    other["node_id"] = 0x3A
    ds.create_from_raw(other, "Back Gate")
    dev_b = devices[ds.lookup(0x3A, 1)]
    _open(plugin_cls, stub, dev, "matterLock")
    clock.now += LIMIT + 1
    _open(plugin_cls, stub, dev_b, "matterLock")               # a different device: no effect on A
    values = _open(plugin_cls, stub, dev, "matterLock")        # A's stale slot replaced
    values["autoRelockTime"] = "90"
    assert _save(plugin_cls, stub, dev, values) == 1
