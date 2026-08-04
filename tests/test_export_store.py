"""The export allow-list store (PRD-indigo-matter-export §5.1 / §4.3).

Covers CRUD, the JSON round trip, schema versioning, snapshot immutability,
lock re-entrancy, and the property that matters most in the field: a blob we
cannot parse is **preserved**, never discarded — a user who has hand-built
twenty exports must be able to get them back.
"""
from __future__ import annotations

import json
import threading

import pytest

from bridge_protocol import ROLES
from export_store import (
    OPTION_INVERT,
    PREF_KEY,
    PREF_KEY_CORRUPT,
    SCHEMA_VERSION,
    ExportEntry,
    ExportStore,
)


@pytest.fixture
def prefs():
    return {}


@pytest.fixture
def store(prefs, mock_logger):
    return ExportStore(prefs, mock_logger)


def _entry(device_id=101, role="onOffLight", **kwargs):
    return ExportEntry(indigo_device_id=device_id, role=role, **kwargs)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def test_new_store_is_empty(store):
    assert store.all() == ()
    assert store.ids() == frozenset()
    assert len(store) == 0


def test_upsert_then_get(store):
    store.upsert(_entry())
    assert store.get(101) == _entry()
    assert store.ids() == frozenset({101})
    assert 101 in store


def test_upsert_replaces_by_device_id(store):
    store.upsert(_entry(role="onOffLight"))
    store.upsert(_entry(role="doorLock"))
    assert len(store) == 1
    assert store.get(101).role == "doorLock"


def test_remove_reports_whether_it_existed(store):
    store.upsert(_entry())
    assert store.remove(101) is True
    assert store.remove(101) is False
    assert store.get(101) is None


def test_all_is_ordered_by_device_id(store):
    for device_id in (300, 100, 200):
        store.upsert(_entry(device_id))
    assert [e.indigo_device_id for e in store.all()] == [100, 200, 300]


def test_contains_tolerates_junk(store):
    assert ("nonsense" in store) is False
    assert (None in store) is False


def test_replace_all_swaps_the_whole_list(store):
    store.upsert(_entry(1))
    store.replace_all([_entry(2), _entry(3)])
    assert store.ids() == frozenset({2, 3})


# ---------------------------------------------------------------------------
# Snapshot immutability
# ---------------------------------------------------------------------------
def test_all_snapshot_does_not_track_later_writes(store):
    store.upsert(_entry(1))
    snapshot = store.all()
    store.upsert(_entry(2))
    assert [e.indigo_device_id for e in snapshot] == [1]


def test_ids_snapshot_is_frozen(store):
    store.upsert(_entry(1))
    ids = store.ids()
    store.remove(1)
    assert ids == frozenset({1})
    with pytest.raises(AttributeError):
        ids.add(2)  # frozenset, by contract


def test_entry_is_immutable(store):
    entry = _entry()
    with pytest.raises(AttributeError):
        entry.role = "doorLock"  # frozen dataclass


# ---------------------------------------------------------------------------
# Persistence / round trip / schema version
# ---------------------------------------------------------------------------
def test_write_persists_immediately_to_prefs(prefs, store):
    store.upsert(_entry(7, "windowCovering", name_override="Blind",
                        options={OPTION_INVERT: True}))
    payload = json.loads(prefs[PREF_KEY])
    assert payload["v"] == SCHEMA_VERSION
    assert payload["exports"] == [{
        "indigoDeviceId": 7, "role": "windowCovering",
        "nameOverride": "Blind", "options": {OPTION_INVERT: True},
    }]


def test_round_trips_through_a_second_store(prefs, mock_logger):
    first = ExportStore(prefs, mock_logger)
    first.upsert(_entry(7, "windowCovering", name_override="Blind",
                        options={OPTION_INVERT: True}))
    first.upsert(_entry(8, "thermostat"))
    second = ExportStore(prefs, mock_logger)
    assert second.all() == first.all()


def test_remove_persists(prefs, store, mock_logger):
    store.upsert(_entry(1))
    store.upsert(_entry(2))
    store.remove(1)
    assert ExportStore(prefs, mock_logger).ids() == frozenset({2})


def test_blank_pref_is_simply_empty(mock_logger):
    assert ExportStore({PREF_KEY: ""}, mock_logger).all() == ()
    mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# Corrupt-blob preservation — never silently discard user config
# ---------------------------------------------------------------------------
def test_unparseable_json_is_preserved_and_store_starts_empty(mock_logger):
    prefs = {PREF_KEY: "{not json at all"}
    store = ExportStore(prefs, mock_logger)
    assert store.all() == ()
    assert prefs[PREF_KEY_CORRUPT] == "{not json at all"
    mock_logger.error.assert_called()


def test_wrong_schema_version_is_preserved_not_reinterpreted(mock_logger):
    blob = json.dumps({"v": 99, "exports": [{"indigoDeviceId": 1, "role": "onOffLight"}]})
    prefs = {PREF_KEY: blob}
    store = ExportStore(prefs, mock_logger)
    assert store.all() == ()
    assert prefs[PREF_KEY_CORRUPT] == blob


def test_non_object_payload_is_preserved(mock_logger):
    prefs = {PREF_KEY: "[1, 2, 3]"}
    assert ExportStore(prefs, mock_logger).all() == ()
    assert prefs[PREF_KEY_CORRUPT] == "[1, 2, 3]"


def test_exports_not_a_list_is_preserved(mock_logger):
    blob = json.dumps({"v": SCHEMA_VERSION, "exports": {"nope": True}})
    prefs = {PREF_KEY: blob}
    assert ExportStore(prefs, mock_logger).all() == ()
    assert prefs[PREF_KEY_CORRUPT] == blob


def test_one_bad_entry_drops_only_itself_and_keeps_the_blob(mock_logger):
    blob = json.dumps({"v": SCHEMA_VERSION, "exports": [
        {"indigoDeviceId": 1, "role": "onOffLight"},
        {"indigoDeviceId": 2, "role": "teleporter"},   # role not in §4.2
        {"role": "onOffLight"},                        # no device id
        {"indigoDeviceId": 3, "role": "thermostat"},
    ]})
    prefs = {PREF_KEY: blob}
    store = ExportStore(prefs, mock_logger)
    assert store.ids() == frozenset({1, 3})
    assert prefs[PREF_KEY_CORRUPT] == blob
    assert mock_logger.error.call_count == 2


def test_entry_from_dict_rejects_unknown_role():
    with pytest.raises(ValueError):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": "notARole"})


def test_entry_from_dict_accepts_every_protocol_role():
    for role in ROLES:
        entry = ExportEntry.from_dict({"indigoDeviceId": 1, "role": role})
        assert entry.role == role


def test_entry_from_dict_rejects_non_object():
    with pytest.raises(ValueError):
        ExportEntry.from_dict("nope")


def test_entry_from_dict_rejects_bad_options():
    with pytest.raises(ValueError):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": "onOffLight", "options": "nope"})


def test_entry_from_dict_rejects_non_string_name_override():
    with pytest.raises(ValueError):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": "onOffLight", "nameOverride": 7})


def test_preservation_failure_does_not_raise(mock_logger):
    class HostileMapping(dict):
        def __setitem__(self, key, value):
            if key == PREF_KEY_CORRUPT:
                raise RuntimeError("prefs are read-only")
            super().__setitem__(key, value)

    prefs = HostileMapping({PREF_KEY: "{broken"})
    store = ExportStore(prefs, mock_logger)  # must not raise
    assert store.all() == ()
    mock_logger.exception.assert_called()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def test_lock_is_reentrant(store):
    # An RLock, not a Lock: upsert() persists via all(), which re-acquires.
    assert isinstance(store._lock, type(threading.RLock()))


def test_public_calls_nest_without_deadlocking(store):
    with store._lock:
        store.upsert(_entry(1))
        assert store.get(1) is not None
        assert store.all()
        assert store.ids() == frozenset({1})
        assert store.remove(1) is True


def test_concurrent_writers_all_land(prefs, mock_logger):
    store = ExportStore(prefs, mock_logger)

    def writer(base):
        for offset in range(20):
            store.upsert(_entry(base + offset))

    threads = [threading.Thread(target=writer, args=(base,)) for base in (1000, 2000, 3000)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store) == 60
    assert len(json.loads(prefs[PREF_KEY])["exports"]) == 60


def test_label_for_prefers_the_override():
    assert _entry().label_for("Kitchen Lamp") == "Kitchen Lamp"
    assert _entry(name_override="Hall").label_for("Kitchen Lamp") == "Hall"
