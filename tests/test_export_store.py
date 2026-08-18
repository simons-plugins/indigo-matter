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
    LOAD_ERROR_UNREADABLE,
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
    return ExportStore(lambda: prefs, mock_logger)


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
        "publishedAs": None,
    }]


def test_round_trips_through_a_second_store(prefs, mock_logger):
    first = ExportStore(lambda: prefs, mock_logger)
    first.upsert(_entry(7, "windowCovering", name_override="Blind",
                        options={OPTION_INVERT: True}))
    first.upsert(_entry(8, "thermostat"))
    second = ExportStore(lambda: prefs, mock_logger)
    assert second.all() == first.all()


def test_remove_persists(prefs, store, mock_logger):
    store.upsert(_entry(1))
    store.upsert(_entry(2))
    store.remove(1)
    assert ExportStore(lambda: prefs, mock_logger).ids() == frozenset({2})


def test_blank_pref_is_simply_empty(mock_logger):
    assert ExportStore(lambda: {PREF_KEY: ""}, mock_logger).all() == ()
    mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# S1 — persist-then-commit: memory and prefs may never diverge
# ---------------------------------------------------------------------------
class _Flush:
    """A save_prefs stand-in that can be made to fail, and records its order."""

    def __init__(self, prefs, fail=False):
        self._prefs = prefs
        self.fail = fail
        self.saw = []

    def __call__(self):
        # Captured at flush time: proves the pref was written BEFORE the flush.
        self.saw.append(self._prefs.get(PREF_KEY))
        if self.fail:
            raise RuntimeError("Indigo refused the write")


def test_commit_writes_the_pref_before_flushing(prefs, mock_logger):
    flush = _Flush(prefs)
    store = ExportStore(lambda: prefs, mock_logger, save_prefs=flush)
    store.upsert(_entry(1))
    assert len(flush.saw) == 1
    assert "indigoDeviceId" in flush.saw[0]


def test_upsert_that_fails_to_flush_changes_nothing(prefs, mock_logger):
    flush = _Flush(prefs)
    store = ExportStore(lambda: prefs, mock_logger, save_prefs=flush)
    store.upsert(_entry(1))
    good_blob = prefs[PREF_KEY]

    flush.fail = True
    with pytest.raises(RuntimeError):
        store.upsert(_entry(2))
    # Memory unchanged...
    assert store.ids() == frozenset({1})
    # ...and so is the pref, so a restart agrees with the dialog.
    assert prefs[PREF_KEY] == good_blob
    assert ExportStore(lambda: prefs, mock_logger).ids() == frozenset({1})


def test_remove_that_fails_to_flush_does_not_resurrect_the_device(prefs, mock_logger):
    """The probe case: a failed remove-save must not delete it from memory only.

    Pre-#122 the entry vanished from the dialog and came back on restart.
    """
    flush = _Flush(prefs)
    store = ExportStore(lambda: prefs, mock_logger, save_prefs=flush)
    store.upsert(_entry(1))
    store.upsert(_entry(2))

    flush.fail = True
    with pytest.raises(RuntimeError):
        store.remove(2)
    assert store.ids() == frozenset({1, 2})
    assert ExportStore(lambda: prefs, mock_logger).ids() == frozenset({1, 2})


def test_first_ever_write_that_fails_leaves_no_pref_key(prefs, mock_logger):
    flush = _Flush(prefs, fail=True)
    store = ExportStore(lambda: prefs, mock_logger, save_prefs=flush)
    with pytest.raises(RuntimeError):
        store.upsert(_entry(1))
    assert PREF_KEY not in prefs
    assert store.all() == ()


def test_replace_all_that_fails_to_flush_keeps_the_old_list(prefs, mock_logger):
    flush = _Flush(prefs)
    store = ExportStore(lambda: prefs, mock_logger, save_prefs=flush)
    store.replace_all([_entry(1), _entry(2)])
    flush.fail = True
    with pytest.raises(RuntimeError):
        store.replace_all([_entry(9)])
    assert store.ids() == frozenset({1, 2})


def test_removing_something_absent_does_not_flush(prefs, mock_logger):
    flush = _Flush(prefs)
    store = ExportStore(lambda: prefs, mock_logger, save_prefs=flush)
    assert store.remove(42) is False
    assert flush.saw == []


# ---------------------------------------------------------------------------
# S2 — prefs are resolved late (Indigo can rebind pluginPrefs)
# ---------------------------------------------------------------------------
def test_writes_follow_a_rebound_prefs_mapping(mock_logger):
    """A PluginConfig save can hand the plugin a NEW pluginPrefs object.

    The store must write to whatever ``prefs_getter`` returns now, not to the
    mapping it happened to see at startup — otherwise every export lands on an
    orphan dict nobody persists.
    """
    holder = {"prefs": {}}
    store = ExportStore(lambda: holder["prefs"], mock_logger)
    store.upsert(_entry(1))
    original = holder["prefs"]

    holder["prefs"] = {}          # Indigo rebinds self.pluginPrefs
    store.upsert(_entry(2))

    assert PREF_KEY in holder["prefs"]
    assert len(json.loads(holder["prefs"][PREF_KEY])["exports"]) == 2
    assert json.loads(original[PREF_KEY])["exports"][0]["indigoDeviceId"] == 1


def test_load_reads_through_the_getter_too(mock_logger):
    prefs = {}
    ExportStore(lambda: prefs, mock_logger).upsert(_entry(5))
    assert ExportStore(lambda: prefs, mock_logger).ids() == frozenset({5})


# ---------------------------------------------------------------------------
# Corrupt-blob preservation — never silently discard user config
# ---------------------------------------------------------------------------
def test_unparseable_json_is_preserved_and_store_starts_empty(mock_logger):
    prefs = {PREF_KEY: "{not json at all"}
    store = ExportStore(lambda: prefs, mock_logger)
    assert store.all() == ()
    assert prefs[PREF_KEY_CORRUPT] == "{not json at all"
    mock_logger.error.assert_called()


def test_wrong_schema_version_is_preserved_not_reinterpreted(mock_logger):
    blob = json.dumps({"v": 99, "exports": [{"indigoDeviceId": 1, "role": "onOffLight"}]})
    prefs = {PREF_KEY: blob}
    store = ExportStore(lambda: prefs, mock_logger)
    assert store.all() == ()
    assert prefs[PREF_KEY_CORRUPT] == blob


def test_non_object_payload_is_preserved(mock_logger):
    prefs = {PREF_KEY: "[1, 2, 3]"}
    assert ExportStore(lambda: prefs, mock_logger).all() == ()
    assert prefs[PREF_KEY_CORRUPT] == "[1, 2, 3]"


def test_exports_not_a_list_is_preserved(mock_logger):
    blob = json.dumps({"v": SCHEMA_VERSION, "exports": {"nope": True}})
    prefs = {PREF_KEY: blob}
    assert ExportStore(lambda: prefs, mock_logger).all() == ()
    assert prefs[PREF_KEY_CORRUPT] == blob


def test_one_bad_entry_drops_only_itself_and_keeps_the_blob(mock_logger):
    blob = json.dumps({"v": SCHEMA_VERSION, "exports": [
        {"indigoDeviceId": 1, "role": "onOffLight"},
        {"indigoDeviceId": 2, "role": "teleporter"},   # role not in §4.2
        {"role": "onOffLight"},                        # no device id
        {"indigoDeviceId": 3, "role": "thermostat"},
    ]})
    prefs = {PREF_KEY: blob}
    store = ExportStore(lambda: prefs, mock_logger)
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


# ---------------------------------------------------------------------------
# Issues #219/#240 — published_as
# ---------------------------------------------------------------------------
def test_entry_from_dict_missing_published_as_is_none():
    """A payload written by an older (pre-PR5) plugin has no key at all."""
    entry = ExportEntry.from_dict({"indigoDeviceId": 1, "role": "onOffLight"})
    assert entry.published_as is None


def test_entry_from_dict_accepts_the_default_published_as():
    entry = ExportEntry.from_dict(
        {"indigoDeviceId": 1, "role": "onOffLight", "publishedAs": "indigo-1"})
    assert entry.published_as == "indigo-1"


def test_entry_from_dict_accepts_a_generation_suffixed_published_as():
    entry = ExportEntry.from_dict(
        {"indigoDeviceId": 1, "role": "onOffLight", "publishedAs": "indigo-1~2"})
    assert entry.published_as == "indigo-1~2"


def test_entry_from_dict_rejects_malformed_published_as():
    with pytest.raises(ValueError):
        ExportEntry.from_dict(
            {"indigoDeviceId": 1, "role": "onOffLight", "publishedAs": "not-lawful"})


def test_entry_from_dict_accepts_a_re_adopted_published_as():
    """Issue #219 — a re-adopted entry publishes as the identity of the OLD,
    deleted device while being driven by a NEW one. The two deliberately do
    NOT agree, and demanding they did dropped the entry on the next load."""
    entry = ExportEntry.from_dict(
        {"indigoDeviceId": 1, "role": "onOffLight", "publishedAs": "indigo-2"})
    assert entry.published_as == "indigo-2"


def test_entry_from_dict_rejects_non_string_published_as():
    with pytest.raises(ValueError):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": "onOffLight", "publishedAs": 7})


def test_published_as_round_trips_through_a_second_store(prefs, mock_logger):
    first = ExportStore(lambda: prefs, mock_logger)
    first.upsert(_entry(7, "windowCovering", published_as="indigo-7~2"))
    second = ExportStore(lambda: prefs, mock_logger)
    assert second.get(7).published_as == "indigo-7~2"


def test_a_re_adopted_entry_round_trips_through_a_second_store(prefs, mock_logger):
    """Issue #219, end to end through the persistence the menu action uses:
    `menuReadoptExport` writes exactly this shape (a NEW device publishing as
    the OLD device's identity), so it has to survive a plugin restart — a
    dropped entry here silently un-exports the accessory the re-adopt existed
    to preserve."""
    first = ExportStore(lambda: prefs, mock_logger)
    first.upsert(_entry(987654321, "onOffLight", published_as="indigo-123456789"))
    second = ExportStore(lambda: prefs, mock_logger)
    assert second.load_error is None
    assert second.get(987654321).published_as == "indigo-123456789"


def test_published_as_absent_on_load_of_an_older_payload(mock_logger):
    """An entry saved before PR5 has no `publishedAs` key at all — it must
    load, not be dropped as unusable, with `published_as` defaulting to None."""
    blob = json.dumps({"v": SCHEMA_VERSION, "exports": [
        {"indigoDeviceId": 1, "role": "onOffLight", "nameOverride": None, "options": {}},
    ]})
    store = ExportStore(lambda: {PREF_KEY: blob}, mock_logger)
    assert store.load_error is None
    assert store.get(1).published_as is None


# ---------------------------------------------------------------------------
# S3 — load_error: the dialog must not claim an empty list is intentional
# ---------------------------------------------------------------------------
def test_clean_load_sets_no_load_error(prefs, mock_logger):
    ExportStore(lambda: prefs, mock_logger).upsert(_entry(1))
    assert ExportStore(lambda: prefs, mock_logger).load_error is None


def test_corrupt_blob_sets_the_load_error(mock_logger):
    prefs = {PREF_KEY: "{not json at all"}
    store = ExportStore(lambda: prefs, mock_logger)
    assert store.load_error == LOAD_ERROR_UNREADABLE
    assert "preserved" in store.load_error


def test_dropped_rows_set_a_counted_load_error(mock_logger):
    blob = json.dumps({"v": SCHEMA_VERSION, "exports": [
        {"indigoDeviceId": 1, "role": "onOffLight"},
        {"indigoDeviceId": 2, "role": "teleporter"},
    ]})
    store = ExportStore(lambda: {PREF_KEY: blob}, mock_logger)
    assert store.load_error is not None
    assert "1 saved export(s)" in store.load_error


# ---------------------------------------------------------------------------
# S4 — the rescue copy: first one wins, and nothing later clobbers it
# ---------------------------------------------------------------------------
def test_rescue_copy_survives_a_later_successful_save(mock_logger):
    prefs = {PREF_KEY: "{broken"}
    store = ExportStore(lambda: prefs, mock_logger)
    rescued = prefs[PREF_KEY_CORRUPT]
    store.upsert(_entry(1))              # the user rebuilds one export
    assert prefs[PREF_KEY_CORRUPT] == rescued
    assert "indigoDeviceId" in prefs[PREF_KEY]


def test_second_corruption_does_not_overwrite_the_first_rescue(mock_logger):
    prefs = {PREF_KEY: "ORIGINAL-twenty-exports"}
    ExportStore(lambda: prefs, mock_logger)
    assert prefs[PREF_KEY_CORRUPT] == "ORIGINAL-twenty-exports"

    prefs[PREF_KEY] = "second-mangled-blob"
    mock_logger.reset_mock()
    ExportStore(lambda: prefs, mock_logger)
    assert prefs[PREF_KEY_CORRUPT] == "ORIGINAL-twenty-exports"
    # Never silent: the user is told the newer blob was NOT kept.
    assert any("NOT preserved" in str(call) for call in mock_logger.error.call_args_list)


# ---------------------------------------------------------------------------
# S5 — restored entries are re-validated (load is an unguarded write path)
# ---------------------------------------------------------------------------
def _blob(*entries):
    return json.dumps({"v": SCHEMA_VERSION, "exports": list(entries)})


def test_validator_drops_a_rejected_entry_and_preserves_the_blob(mock_logger):
    blob = _blob({"indigoDeviceId": 1, "role": "onOffLight"},
                 {"indigoDeviceId": 2, "role": "onOffLight"})
    prefs = {PREF_KEY: blob}
    store = ExportStore(
        lambda: prefs, mock_logger,
        entry_validator=lambda e: "created by this plugin (loop guard)"
        if e.indigo_device_id == 2 else None,
    )
    assert store.ids() == frozenset({1})
    assert prefs[PREF_KEY_CORRUPT] == blob      # recoverable, as with a bad row
    assert store.load_error is not None
    mock_logger.error.assert_called()


def test_validator_that_raises_keeps_the_entry_and_logs(mock_logger):
    """A broken validator must not silently empty the user's allow-list."""
    def boom(_entry):
        raise RuntimeError("indigo.devices exploded")

    store = ExportStore(lambda: {PREF_KEY: _blob({"indigoDeviceId": 1, "role": "onOffLight"})},
                        mock_logger, entry_validator=boom)
    assert store.ids() == frozenset({1})
    mock_logger.exception.assert_called()


def test_no_validator_accepts_everything(mock_logger):
    store = ExportStore(lambda: {PREF_KEY: _blob({"indigoDeviceId": 1, "role": "onOffLight"})},
                        mock_logger)
    assert store.ids() == frozenset({1})


def test_from_dict_rejects_invert_on_a_role_with_no_polarity():
    with pytest.raises(ValueError, match="polarity"):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": "doorLock",
                               "options": {OPTION_INVERT: True}})


def test_from_dict_accepts_invert_on_a_window_covering():
    entry = ExportEntry.from_dict({"indigoDeviceId": 1, "role": "windowCovering",
                                   "options": {OPTION_INVERT: True}})
    assert entry.options == {OPTION_INVERT: True}


def test_from_dict_rejects_a_non_boolean_invert():
    with pytest.raises(ValueError, match="boolean"):
        ExportEntry.from_dict({"indigoDeviceId": 1, "role": "windowCovering",
                               "options": {OPTION_INVERT: "yes"}})


def test_preservation_failure_does_not_raise(mock_logger):
    class HostileMapping(dict):
        def __setitem__(self, key, value):
            if key == PREF_KEY_CORRUPT:
                raise RuntimeError("prefs are read-only")
            super().__setitem__(key, value)

    prefs = HostileMapping({PREF_KEY: "{broken"})
    store = ExportStore(lambda: prefs, mock_logger)  # must not raise
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
    store = ExportStore(lambda: prefs, mock_logger)

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


def test_export_entry_did_not_grow_a_battery_field():
    """Issue #220: battery is automatic and evidence-driven (ADR-0003) — the
    device's own `batteryLevel` is the authority, never a user declaration.
    A tick-box here would let someone assert a battery that does not exist,
    so `ExportEntry`'s shape must stay exactly what it was.
    """
    import dataclasses
    assert "battery" not in {field.name for field in dataclasses.fields(ExportEntry)}
