"""Fabric backup & restore (issue #26) — pure, deterministic unit tests.

Operates entirely on ``tmp_path`` with an injected ``now`` clock and a fake
``server_control`` that records stop()/start() ordering. No real launchctl, no
real matter-server, no sleeps.
"""
from __future__ import annotations

import os
import zipfile
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import fabric_backup

_IDENTITY_JSON = '{"installId": "abc", "passcode": 20202021, "discriminator": 3840}'
_ENDPOINT_MAP_JSON = '{"version": 1, "endpoints": {"indigo-101": 2}}'


def _make_storage(tmp_path, *, files: dict[str, str] | None = None):
    """Create a fake matter-server storage dir with some fabric files."""
    storage = tmp_path / "appsupport" / "matter-server"
    storage.mkdir(parents=True)
    files = files or {
        "config": "fabric-config",
        "certificates/root.pem": "ROOTCERT",
        "server-1-fff1/node.json": '{"node": 1}',
    }
    for rel, content in files.items():
        path = storage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return str(storage)


class FakeControl:
    """Records stop()/start() call order with configurable bool/raise behaviour.

    ``stop_returns`` / ``start_returns`` set the bool the respective method returns
    (default True). ``fail_start`` makes only the FIRST start() RAISE (the rollback
    start then succeeds) — exercising the exception path. ``start_returns_false``
    makes only the first start() RETURN False (the rollback start succeeds) —
    exercising the bool-False path. ``rollback_start_fails`` makes the SECOND
    (rollback) start() return False — exercising the loud manual-recovery path.
    ``rollback_start_raises`` makes the SECOND (rollback) start() RAISE — the
    only path into fabric_backup's rollback except, which no other mode could
    reach (issue #310 audit).
    ``start_always_false`` makes EVERY start() return False — a never-installed
    bridge, whose ``start()`` fails by design and must not abort a good restore.
    ``alive`` is what ``is_alive()`` reports; ``name``/``log`` let a bridge and a
    controller share one ordered call log across #136's two-control tests.
    """

    def __init__(
        self,
        *,
        fail_start: bool = False,
        stop_returns: bool = True,
        start_returns_false: bool = False,
        rollback_start_fails: bool = False,
        rollback_start_raises: bool = False,
        start_always_false: bool = False,
        alive: bool = True,
        name: str = "",
        log: list[str] | None = None,
    ):
        self.calls: list[str] = []
        self._fail_start = fail_start
        self._stop_returns = stop_returns
        self._start_returns_false = start_returns_false
        self._rollback_start_fails = rollback_start_fails
        self._rollback_start_raises = rollback_start_raises
        self._start_always_false = start_always_false
        self._alive = alive
        self._name = name
        self._log = log
        self._start_count = 0

    def _note(self, method: str) -> None:
        if self._log is not None:
            self._log.append(f"{self._name}.{method}")

    def stop(self) -> bool:
        self.calls.append("stop")
        self._note("stop")
        return self._stop_returns

    def start(self) -> bool:
        self.calls.append("start")
        self._note("start")
        self._start_count += 1
        is_first = self._start_count == 1
        if self._start_always_false:
            return False
        if is_first and self._fail_start:
            raise RuntimeError("boom on start")
        if is_first and self._start_returns_false:
            return False
        if not is_first and self._rollback_start_raises:
            raise RuntimeError("boom on rollback start")
        if not is_first and self._rollback_start_fails:
            return False
        return True

    def is_alive(self) -> bool:
        # Deliberately does NOT append to .calls (it is not stop()/start()) but
        # DOES append to the shared log, so ordering tests can filter it out.
        if self._log is not None:
            self._log.append(f"{self._name}.is_alive")
        return self._alive


def _two_controls(**bridge_kwargs):
    """A shared ordered-call log plus a named ("ctl", "bridge") FakeControl pair."""
    log: list[str] = []
    ctl = FakeControl(name="ctl", log=log)
    bridge = FakeControl(name="bridge", log=log, **bridge_kwargs)
    return log, ctl, bridge


class FakeLogger:
    """Captures log records by level so tests can assert on escalations."""

    def __init__(self):
        self.records: dict[str, list[str]] = {"info": [], "warning": [], "error": [], "debug": []}

    def _record(self, level, msg, *args):
        try:
            self.records[level].append(msg % args if args else msg)
        except Exception:  # never let logging break a test
            self.records[level].append(str(msg))

    def info(self, msg, *args):
        self._record("info", msg, *args)

    def warning(self, msg, *args):
        self._record("warning", msg, *args)

    def error(self, msg, *args):
        self._record("error", msg, *args)

    def debug(self, msg, *args):
        self._record("debug", msg, *args)

    def text(self, level: str) -> str:
        return "\n".join(self.records[level])


_NOW = datetime(2026, 6, 10, 12, 41, 45, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# backups_dir_for
# ----------------------------------------------------------------------
def test_backups_dir_is_sibling_of_storage(tmp_path):
    storage = _make_storage(tmp_path)
    backups = fabric_backup.backups_dir_for(storage)
    # sibling "backups" dir, NOT inside the storage dir
    assert backups == os.path.join(os.path.dirname(storage), "backups")
    assert not backups.startswith(storage + os.sep)


# ----------------------------------------------------------------------
# create_backup
# ----------------------------------------------------------------------
def test_create_backup_writes_zip_and_returns_path(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    assert archive.endswith("fabric-20260610T124145Z.zip")
    assert os.path.isfile(archive)
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    # stored relative to the storage root
    assert "config" in names
    assert "certificates/root.pem" in names
    assert "server-1-fff1/node.json" in names


def test_create_backup_raises_on_missing_storage(tmp_path):
    with pytest.raises(FileNotFoundError):
        fabric_backup.create_backup(str(tmp_path / "nope"), now=_NOW)


def test_create_backup_raises_on_empty_storage(tmp_path):
    storage = tmp_path / "matter-server"
    storage.mkdir()
    with pytest.raises(FileNotFoundError):
        fabric_backup.create_backup(str(storage), now=_NOW)


# ----------------------------------------------------------------------
# list_backups
# ----------------------------------------------------------------------
def test_list_backups_ordering_and_fields(tmp_path):
    storage = _make_storage(tmp_path)
    backups_dir = fabric_backup.backups_dir_for(storage)
    os.makedirs(backups_dir)
    older = os.path.join(backups_dir, "fabric-20260101T000000Z.zip")
    newer = os.path.join(backups_dir, "fabric-20260601T000000Z.zip")
    for path in (older, newer):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("config", "x")
    # set distinct mtimes (older actually older)
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    # a non-matching file must be ignored
    with open(os.path.join(backups_dir, "readme.txt"), "w") as fh:
        fh.write("ignore me")

    listing = fabric_backup.list_backups(storage)
    assert [e["filename"] for e in listing] == [
        "fabric-20260601T000000Z.zip",
        "fabric-20260101T000000Z.zip",
    ]
    entry = listing[0]
    assert set(entry) == {"filename", "path", "size_bytes", "mtime"}
    assert entry["size_bytes"] > 0


def test_list_backups_empty_when_no_dir(tmp_path):
    storage = _make_storage(tmp_path)
    assert fabric_backup.list_backups(storage) == []


# ----------------------------------------------------------------------
# prune_backups
# ----------------------------------------------------------------------
def test_prune_keeps_newest_n(tmp_path):
    storage = _make_storage(tmp_path)
    backups_dir = fabric_backup.backups_dir_for(storage)
    os.makedirs(backups_dir)
    names = [f"fabric-202601{day:02d}T000000Z.zip" for day in range(1, 16)]  # 15 backups
    for i, name in enumerate(names):
        path = os.path.join(backups_dir, name)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("config", "x")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))

    removed = fabric_backup.prune_backups(storage, keep=10)
    assert len(removed) == 5
    # the 5 oldest were removed
    assert set(removed) == set(names[:5])
    remaining = {e["filename"] for e in fabric_backup.list_backups(storage)}
    assert remaining == set(names[5:])


def test_create_backup_prunes_to_ten(tmp_path):
    storage = _make_storage(tmp_path)
    backups_dir = fabric_backup.backups_dir_for(storage)
    os.makedirs(backups_dir)
    # pre-seed 10 existing backups all older than the new one
    for i in range(10):
        path = os.path.join(backups_dir, f"fabric-202601{i + 1:02d}T000000Z.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("config", "x")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    archive = fabric_backup.create_backup(storage, now=_NOW)  # 11th, newest
    listing = fabric_backup.list_backups(storage)
    assert len(listing) == 10
    assert os.path.basename(archive) == listing[0]["filename"]


# ----------------------------------------------------------------------
# restore_backup — round trip + safety
# ----------------------------------------------------------------------
def test_restore_round_trip_into_fresh_dir(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    # restore into a brand-new (non-existent) storage location
    fresh = str(tmp_path / "restored" / "matter-server")
    control = FakeControl()
    result = fabric_backup.restore_backup(archive, fresh, control, now=_NOW)
    assert result["restored_from"] == archive
    assert result["moved_aside_to"] is None  # nothing to move aside
    assert (tmp_path / "restored" / "matter-server" / "config").read_text() == "fabric-config"
    assert (tmp_path / "restored" / "matter-server" / "certificates" / "root.pem").read_text() == "ROOTCERT"
    assert control.calls == ["stop", "start"]


def test_restore_moves_existing_aside_and_orders_stop_then_start(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    # mutate the live storage so we can prove the backup (not the live dir) won
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("MUTATED")
    control = FakeControl()
    result = fabric_backup.restore_backup(archive, storage, control, now=_NOW)

    moved = result["moved_aside_to"]
    assert moved is not None
    assert ".pre-restore-20260610T124145Z" in moved
    # the moved-aside copy still exists (never deleted in place) and holds the mutated content
    assert os.path.isdir(moved)
    assert (tmp_path / "appsupport" / f"matter-server.pre-restore-20260610T124145Z" / "config").read_text() == "MUTATED"
    # restored storage holds the original backed-up content
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "fabric-config"
    # stop happened before start
    assert control.calls == ["stop", "start"]


def test_restore_rolls_back_on_start_failure(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    control = FakeControl(fail_start=True)

    with pytest.raises(RuntimeError, match="rolled back"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW)

    # original fabric restored in place (the move-aside was moved back)
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    # the .pre-restore-* copy was consumed by the rollback (moved back)
    assert not os.path.isdir(str(storage) + ".pre-restore-20260610T124145Z")
    # stop, failed start, then rollback start attempted
    assert control.calls == ["stop", "start", "start"]


def test_restore_rejects_missing_archive(tmp_path):
    storage = _make_storage(tmp_path)
    control = FakeControl()
    with pytest.raises(FileNotFoundError):
        fabric_backup.restore_backup(str(tmp_path / "nope.zip"), storage, control, now=_NOW)
    # server was never touched
    assert control.calls == []


def test_restore_rejects_non_zip(tmp_path):
    storage = _make_storage(tmp_path)
    bogus = tmp_path / "bogus.zip"
    bogus.write_text("not a zip")
    control = FakeControl()
    with pytest.raises(ValueError):
        fabric_backup.restore_backup(str(bogus), storage, control, now=_NOW)
    assert control.calls == []


def test_restore_rejects_zip_slip_member(tmp_path):
    storage = _make_storage(tmp_path)
    # craft a malicious archive whose member escapes the extraction root
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "pwned")
    fresh = str(tmp_path / "restore-target")
    control = FakeControl()
    with pytest.raises((ValueError, RuntimeError), match="zip-slip|rolled back"):
        fabric_backup.restore_backup(str(evil), fresh, control, now=_NOW)
    # the escape file must NOT have been written outside the target
    assert not (tmp_path / "escape.txt").exists()
    # the zip-slip pre-flight fires before either control is touched
    assert control.calls == []


# ----------------------------------------------------------------------
# C1 — stop()/start() bool returns are honoured
# ----------------------------------------------------------------------
def test_restore_aborts_before_touching_fabric_when_stop_fails(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    control = FakeControl(stop_returns=False)

    with pytest.raises(RuntimeError, match="failed to stop"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW)

    # storage is completely untouched: original content intact, nothing moved aside.
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    assert not os.path.isdir(str(storage) + ".pre-restore-20260610T124145Z")
    assert control.calls == ["stop"]  # start never reached


def test_restore_rolls_back_when_start_returns_false(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    # start() RETURNS False (does not raise) on the first call ⇒ treated as failure.
    control = FakeControl(start_returns_false=True)

    with pytest.raises(RuntimeError, match="rolled back"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW)

    # original fabric restored in place, move-aside consumed.
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    assert not os.path.isdir(str(storage) + ".pre-restore-20260610T124145Z")
    # stop, failed (False) start, then a rollback start that succeeds.
    assert control.calls == ["stop", "start", "start"]


# ----------------------------------------------------------------------
# C2 — rollback cannot strand the original even if the partial dir is undeletable
# ----------------------------------------------------------------------
def test_restore_rollback_moves_partial_aside_when_not_trivially_removable(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    logger = FakeLogger()
    control = FakeControl(fail_start=True)  # forces rollback

    with pytest.raises(RuntimeError, match="rolled back"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW, logger=logger)

    # The original fabric is back in place (moved back), NOT stranded at pre-restore.
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    assert not os.path.isdir(str(storage) + ".pre-restore-20260610T124145Z")
    # rollback re-attempted start on the restored fabric.
    assert control.calls == ["stop", "start", "start"]
    # The partial new dir was moved aside to a .failed-* path (not silently rmtree'd).
    parent = os.path.dirname(storage)
    failed = [n for n in os.listdir(parent) if ".failed-" in n]
    assert failed, "partial failed restore should have been moved aside to a .failed-* path"


def test_restore_rollback_start_failure_logs_manual_recovery(tmp_path):
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    logger = FakeLogger()
    # First start returns False (restore fails) AND the rollback start also fails.
    control = FakeControl(start_returns_false=True, rollback_start_fails=True)

    with pytest.raises(RuntimeError, match="rolled back"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW, logger=logger)

    # original fabric still restored on disk
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    # the loudest case: ERROR with manual-recovery guidance naming the fabric location
    err = logger.text("error")
    assert "CRITICAL" in err
    assert "DOWN" in err
    assert storage in err  # original fabric location named for manual recovery


def test_restore_rollback_start_that_RAISES_still_reaches_manual_recovery(tmp_path):
    """The rollback restart raising, not merely returning False.

    Same worst case as the test above — original fabric back on disk, server
    down — reached the other way. It matters because the two arrive by
    different routes: a False return falls straight into the `if not started`
    block, while a raise has to be CAUGHT first or it escapes past both the
    CRITICAL line and the bridge restart, leaving the user with a traceback
    and no idea where their fabric is. No FakeControl mode could reach that
    except until now (issue #310 audit).
    """
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    logger = FakeLogger()
    control = FakeControl(start_returns_false=True, rollback_start_raises=True)

    with pytest.raises(RuntimeError, match="rolled back"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW, logger=logger)

    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    err = logger.text("error")
    # Two CRITICALs, not one: the raise is named, AND the manual-recovery
    # guidance still fires. Either alone would leave the user short.
    assert "failed to restart after restore rollback" in err
    assert "boom on rollback start" in err, "the caught exception must be named, not just counted"
    assert "DOWN" in err
    assert storage in err


# ----------------------------------------------------------------------
# H4 — empty (but valid) archive must NOT silently wipe the fabric
# ----------------------------------------------------------------------
def test_restore_rejects_zero_member_archive_up_front(tmp_path):
    storage = _make_storage(tmp_path)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w"):
        pass  # a valid zip with zero members
    control = FakeControl()
    with pytest.raises(ValueError, match="no members"):
        fabric_backup.restore_backup(str(empty), storage, control, now=_NOW)
    # fabric NOT wiped, server never touched
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    assert control.calls == []


def test_restore_rolls_back_when_extraction_yields_empty_dir(tmp_path, monkeypatch):
    # Defence in depth: even if extraction somehow produced an empty storage dir
    # (a silent wipe), the post-extract non-empty guard must catch it and roll back
    # rather than leave the user fabric-less. Simulate by no-op-ing _safe_extract.
    storage = _make_storage(tmp_path)
    archive = fabric_backup.create_backup(storage, now=_NOW)
    (tmp_path / "appsupport" / "matter-server" / "config").write_text("ORIGINAL-LIVE")
    monkeypatch.setattr(fabric_backup, "_safe_extract", lambda *_a, **_k: None)
    control = FakeControl()
    with pytest.raises(RuntimeError, match="rolled back"):
        fabric_backup.restore_backup(archive, storage, control, now=_NOW)
    # the original fabric was preserved and restored, NOT wiped
    assert (tmp_path / "appsupport" / "matter-server" / "config").read_text() == "ORIGINAL-LIVE"
    # start() was never called (extract-result failed before start); rollback ran.
    assert control.calls == ["stop", "start"]  # only the rollback start


# ----------------------------------------------------------------------
# C3 — create_backup atomic write + validation
# ----------------------------------------------------------------------
def test_create_backup_leaves_no_archive_on_mid_write_failure(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    backups_dir = fabric_backup.backups_dir_for(storage)

    real_write = zipfile.ZipFile.write
    calls = {"n": 0}

    def exploding_write(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full mid-snapshot")
        return real_write(self, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "write", exploding_write)
    with pytest.raises(OSError, match="disk full"):
        fabric_backup.create_backup(storage, now=_NOW)

    # No fabric-*.zip and no leftover .tmp — the picker must never see a partial.
    leftovers = os.listdir(backups_dir) if os.path.isdir(backups_dir) else []
    assert not any(n.startswith("fabric-") for n in leftovers), leftovers
    assert not any(n.endswith(".tmp") for n in leftovers), leftovers


def test_create_backup_writes_atomic_final_name_and_validates(tmp_path):
    storage = _make_storage(tmp_path)
    logger = FakeLogger()
    archive = fabric_backup.create_backup(storage, now=_NOW, logger=logger)
    # final name, no .tmp left behind, and it is a clean readable zip
    assert archive.endswith("fabric-20260610T124145Z.zip")
    assert os.path.isfile(archive)
    assert not os.path.exists(archive + ".tmp")
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
        assert zf.namelist()  # non-empty
    assert "member" in logger.text("info")


# ----------------------------------------------------------------------
# M6 — prune logs undeletable backups via the injected logger
# ----------------------------------------------------------------------
def test_prune_logs_undeletable_backup(tmp_path, monkeypatch):
    storage = _make_storage(tmp_path)
    backups_dir = fabric_backup.backups_dir_for(storage)
    os.makedirs(backups_dir)
    for i in range(3):
        path = os.path.join(backups_dir, f"fabric-202601{i + 1:02d}T000000Z.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("config", "x")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))

    logger = FakeLogger()

    def boom_remove(path):
        raise OSError("permission denied")

    monkeypatch.setattr(fabric_backup.os, "remove", boom_remove)
    removed = fabric_backup.prune_backups(storage, keep=1, logger=logger)
    assert removed == []  # nothing actually removed
    assert "Could not prune" in logger.text("warning")


# ----------------------------------------------------------------------
# C5 — _control_is_alive edge cases (fail-safe: default to alive)
# ----------------------------------------------------------------------
def test_control_is_alive_defaults_true_without_an_is_alive_method():
    class _NoIsAlive:  # a control without the OPTIONAL is_alive() extension
        def stop(self):
            return True

        def start(self):
            return True

    assert fabric_backup._control_is_alive(_NoIsAlive(), FakeLogger()) is True


def test_control_is_alive_treats_a_raising_probe_as_alive_and_logs_debug():
    control = Mock()
    control.is_alive.side_effect = RuntimeError("boom")
    logger = FakeLogger()
    assert fabric_backup._control_is_alive(control, logger) is True
    assert "is_alive" in logger.text("debug")


def _make_bridge_storage(tmp_path, *, files: dict[str, str] | None = None):
    """The export bridge node's storage dir — a SIBLING of the controller's."""
    storage = tmp_path / "appsupport" / "bridge-node"
    storage.mkdir(parents=True)
    files = files or {
        "identity.json": '{"installId": "abc", "passcode": 20202021, "discriminator": 3840}',
        "endpoint-map.json": '{"version": 1, "endpoints": {"indigo-101": 2}}',
        "node-indigo-matter-bridge/state.json": "{}",
    }
    for rel, content in files.items():
        path = storage / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return str(storage)


class TestBridgeStorageInBackups:
    """PRD-indigo-matter-export §4.3: back the bridge node's storage up too.

    Losing that one directory costs two irreplaceable things at once — the
    operational credentials for every ecosystem the bridge is paired into, and
    the ``endpoint-map.json`` whose loss duplicates every exported accessory in
    every one of them.
    """

    def _archive(self, tmp_path, bridge=True):
        storage = _make_storage(tmp_path)
        bridge_path = _make_bridge_storage(tmp_path) if bridge else None
        archive = fabric_backup.create_backup(
            storage, now=datetime(2026, 8, 5, tzinfo=timezone.utc),
            bridge_storage_path=bridge_path)
        return storage, archive

    def test_the_bridge_dir_is_archived_under_its_own_prefix(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        assert f"{fabric_backup.BRIDGE_MEMBER_PREFIX}identity.json" in names
        assert f"{fabric_backup.BRIDGE_MEMBER_PREFIX}endpoint-map.json" in names
        # The prefix is what keeps two sibling directories apart in one archive —
        # and what lets an old, controller-only backup still restore cleanly.
        assert "config" in names
        assert not any(name.startswith(fabric_backup.BRIDGE_MEMBER_PREFIX)
                       for name in ["config", "certificates/root.pem"])

    def test_the_endpoint_map_survives_the_round_trip_byte_for_byte(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        with zipfile.ZipFile(archive) as zf:
            body = zf.read(f"{fabric_backup.BRIDGE_MEMBER_PREFIX}endpoint-map.json")
        assert b'"indigo-101": 2' in body

    def test_a_missing_bridge_dir_is_skipped_not_fatal(self, tmp_path):
        """A user who exports nothing has no bridge storage — and still needs a backup."""
        storage = _make_storage(tmp_path)
        archive = fabric_backup.create_backup(
            storage, now=datetime(2026, 8, 5, tzinfo=timezone.utc),
            bridge_storage_path=str(tmp_path / "nope"))
        with zipfile.ZipFile(archive) as zf:
            assert zf.namelist()
            assert fabric_backup.bridge_members_in(archive) == []

    def test_omitting_the_argument_reproduces_the_old_archive(self, tmp_path):
        _storage, archive = self._archive(tmp_path, bridge=False)
        assert fabric_backup.bridge_members_in(archive) == []

    def test_restore_leaves_the_bridge_members_alone_and_says_so(self, tmp_path):
        """A half-restore nobody mentioned is the worst of the three options.

        Extracting them under the CONTROLLER's storage dir would put a second
        Matter node's credentials where nothing will ever read them; doing it in
        place needs the bridge node stopped, and this restore, when the caller
        supplies no bridge control, has no way to do that.
        So: restore the fabric, and name the files, the prefix and the manual
        recipe.
        """
        storage, archive = self._archive(tmp_path)
        logger = _RecordingLogger()

        fabric_backup.restore_backup(
            archive, storage, FakeControl(),
            now=datetime(2026, 8, 6, tzinfo=timezone.utc), logger=logger)

        assert not os.path.exists(os.path.join(storage, "bridge-node"))
        assert os.path.isfile(os.path.join(storage, "config"))
        warning = " ".join(logger.warnings)
        assert "bridge node" in warning
        assert fabric_backup.BRIDGE_MEMBER_PREFIX in warning

    def test_restoring_a_controller_only_backup_says_nothing_about_the_bridge(self, tmp_path):
        storage, archive = self._archive(tmp_path, bridge=False)
        logger = _RecordingLogger()
        fabric_backup.restore_backup(
            archive, storage, FakeControl(),
            now=datetime(2026, 8, 6, tzinfo=timezone.utc), logger=logger)
        assert "bridge node" not in " ".join(logger.warnings)


class _RecordingLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def _record(self, target, message, *args):
        target.append(str(message) % args if args else str(message))

    def warning(self, message, *args):
        self._record(self.warnings, message, *args)

    def info(self, message, *args):
        pass

    def error(self, message, *args):
        pass

    def debug(self, message, *args):
        pass


class TestBridgeStorageIsAudible:
    """The bridge dir is DERIVED, not configured, so a wrong path is silent.

    The docstring promised "a missing directory is skipped rather than raising",
    and the skip had no log line at all — so a user whose node runs with a
    different `--storage-path` got a plausible success message for an archive
    containing neither `identity.json` nor `endpoint-map.json`. That is exactly
    the archive they will reach for after losing those two files.
    """

    def _storage(self, tmp_path):
        storage = tmp_path / "matter" / "storage"
        storage.mkdir(parents=True)
        (storage / "fabric.json").write_text("{}")
        return storage

    def test_a_missing_bridge_dir_warns_and_names_the_path(self, tmp_path):
        logger = Mock()
        storage = self._storage(tmp_path)
        missing = tmp_path / "matter" / "bridge-node"

        fabric_backup.create_backup(
            str(storage), now=datetime(2026, 8, 5, tzinfo=timezone.utc), logger=logger,
            bridge_storage_path=str(missing))

        said = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                        else str(call.args[0]) for call in logger.warning.call_args_list)
        assert str(missing) in said
        assert "identity.json" in said and "endpoint-map.json" in said

    def test_an_empty_bridge_dir_warns_too(self, tmp_path):
        logger = Mock()
        storage = self._storage(tmp_path)
        empty = tmp_path / "matter" / "bridge-node"
        empty.mkdir()

        fabric_backup.create_backup(
            str(storage), now=datetime(2026, 8, 5, tzinfo=timezone.utc), logger=logger,
            bridge_storage_path=str(empty))

        assert logger.warning.called

    def test_the_success_line_names_the_directory_it_covered(self, tmp_path):
        logger = Mock()
        storage = self._storage(tmp_path)
        bridge = tmp_path / "matter" / "bridge-node"
        bridge.mkdir()
        (bridge / "identity.json").write_text("{}")

        fabric_backup.create_backup(
            str(storage), now=datetime(2026, 8, 5, tzinfo=timezone.utc), logger=logger,
            bridge_storage_path=str(bridge))

        said = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                        else str(call.args[0]) for call in logger.info.call_args_list)
        assert str(bridge) in said
        assert not logger.warning.called


def _bridge_restore_archive(tmp_path, *, bridge=True):
    """A backup archive over the standard fixtures — optionally with bridge members."""
    storage = _make_storage(tmp_path)
    bridge_path = _make_bridge_storage(tmp_path) if bridge else None
    archive = fabric_backup.create_backup(storage, now=_NOW, bridge_storage_path=bridge_path)
    return storage, archive


class TestBridgeRestore:
    """#136 — restore now stops/starts the bridge node and extracts its half too.

    ``BridgeProcess`` gives ``restore_backup`` the ``stop()``/``start()`` seam it
    was missing (E7); this pins the two-control sequence, the extraction, and
    every way it falls back to the controller-only behaviour these tests'
    siblings above already pin.
    """

    def _archive(self, tmp_path, *, bridge=True):
        return _bridge_restore_archive(tmp_path, bridge=bridge)

    def test_bridge_members_extract_into_the_bridge_dir_with_the_prefix_stripped(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        _log, ctl, bridge = _two_controls()

        result = fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is True
        assert result["bridge_members"] == 3
        with open(os.path.join(bridge_dest, "identity.json")) as fh:
            assert fh.read() == _IDENTITY_JSON
        with open(os.path.join(bridge_dest, "endpoint-map.json")) as fh:
            assert fh.read() == _ENDPOINT_MAP_JSON
        with open(os.path.join(bridge_dest, "node-indigo-matter-bridge", "state.json")) as fh:
            assert fh.read() == "{}"

    def test_nothing_named_bridge_node_appears_under_the_controller_storage(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        _log, ctl, bridge = _two_controls()

        fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        # Not nested under the controller's storage, and not double-nested under
        # its own destination either (the prefix-strip pin).
        assert not os.path.exists(os.path.join(fresh_storage, "bridge-node"))
        assert not os.path.exists(os.path.join(bridge_dest, "bridge-node"))

    def test_stop_order_is_bridge_then_controller_and_start_order_is_the_reverse(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log, ctl, bridge = _two_controls()

        fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered == ["bridge.stop", "ctl.stop", "ctl.start", "bridge.start"]

    def test_bridge_stop_failure_aborts_before_the_controller_is_touched(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log, ctl, bridge = _two_controls(stop_returns=False)

        with pytest.raises(RuntimeError, match="bridge node failed to stop"):
            fabric_backup.restore_backup(
                archive, fresh_storage, ctl, now=_NOW,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert not any(entry.startswith("ctl.") for entry in log)
        assert not os.path.exists(fresh_storage)
        assert not os.path.exists(bridge_dest)

    def test_controller_stop_failure_restarts_the_bridge_it_stopped_then_aborts(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log: list[str] = []
        ctl = FakeControl(name="ctl", log=log, stop_returns=False)
        bridge = FakeControl(name="bridge", log=log)

        with pytest.raises(RuntimeError, match="failed to stop"):
            fabric_backup.restore_backup(
                archive, fresh_storage, ctl, now=_NOW,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered == ["bridge.stop", "ctl.stop", "bridge.start"]
        assert not os.path.exists(fresh_storage)
        assert not os.path.exists(bridge_dest)

    def test_controller_stop_failure_logs_when_the_abort_path_bridge_restart_also_fails(self, tmp_path):
        """A2/F2: the abort-path bridge restart result must not be discarded."""
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log: list[str] = []
        ctl = FakeControl(name="ctl", log=log, stop_returns=False)
        bridge = FakeControl(name="bridge", log=log, start_always_false=True)
        logger = FakeLogger()

        with pytest.raises(RuntimeError, match="failed to stop"):
            fabric_backup.restore_backup(
                archive, fresh_storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered == ["bridge.stop", "ctl.stop", "bridge.start"]
        err = logger.text("error")
        assert "bridge" in err.lower()
        assert "did not restart" in err

    def test_a_bridge_that_is_not_running_is_neither_stopped_nor_started(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log, ctl, bridge = _two_controls(alive=False)
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert "bridge.stop" not in log
        assert "bridge.start" not in log
        assert os.path.isfile(os.path.join(bridge_dest, "identity.json"))
        assert result["bridge_started"] is None
        assert "left stopped" in logger.text("info")

    def test_a_bridge_that_cannot_start_does_not_fail_an_otherwise_good_restore(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        _log, ctl, bridge = _two_controls(start_always_false=True)
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is True
        assert result["bridge_started"] is False
        assert os.path.isfile(os.path.join(fresh_storage, "config"))
        err = logger.text("error")
        assert bridge_dest in err
        parent = os.path.dirname(bridge_dest)
        assert not any(".failed-" in name for name in os.listdir(parent))

    def test_a_failing_bridge_extract_rolls_both_dirs_back(self, tmp_path, monkeypatch):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        log, ctl, bridge = _two_controls()

        real_extract = fabric_backup._safe_extract

        def flaky_extract(archive_path, dest, *, prefix=""):
            if prefix:
                raise RuntimeError("boom on bridge extract")
            return real_extract(archive_path, dest, prefix=prefix)

        monkeypatch.setattr(fabric_backup, "_safe_extract", flaky_extract)

        with pytest.raises(RuntimeError, match="rolled back"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        with open(os.path.join(storage, "config")) as fh:
            assert fh.read() == "fabric-config"
        with open(os.path.join(bridge_dest, "identity.json")) as fh:
            assert fh.read() == _IDENTITY_JSON
        parent = os.path.dirname(bridge_dest)
        assert any(name.startswith("bridge-node.failed-") for name in os.listdir(parent))
        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered[-2:] == ["ctl.start", "bridge.start"]

    def test_a_failing_controller_extract_rolls_the_bridge_back_too(self, tmp_path, monkeypatch):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        _log, ctl, bridge = _two_controls()

        real_extract = fabric_backup._safe_extract

        def flaky_extract(archive_path, dest, *, prefix=""):
            if not prefix:
                raise RuntimeError("boom on controller extract")
            return real_extract(archive_path, dest, prefix=prefix)

        monkeypatch.setattr(fabric_backup, "_safe_extract", flaky_extract)

        with pytest.raises(RuntimeError, match="rolled back"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        with open(os.path.join(bridge_dest, "identity.json")) as fh:
            assert fh.read() == _IDENTITY_JSON
        parent = os.path.dirname(bridge_dest)
        assert not any(name.startswith("bridge-node.pre-restore-") for name in os.listdir(parent))

    def test_the_bridge_dir_is_moved_aside_never_deleted(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        # mutate the live bridge storage so we can prove the backup (not the live
        # dir) won, same discipline as the controller's move-aside test above.
        with open(os.path.join(bridge_dest, "identity.json"), "w") as fh:
            fh.write("MUTATED")
        _log, ctl, bridge = _two_controls()

        result = fabric_backup.restore_backup(
            archive, storage, ctl, now=_NOW,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        moved = result["bridge_moved_aside_to"]
        assert moved is not None
        assert os.path.isdir(moved)
        with open(os.path.join(moved, "identity.json")) as fh:
            assert fh.read() == "MUTATED"
        with open(os.path.join(bridge_dest, "identity.json")) as fh:
            assert fh.read() == _IDENTITY_JSON

    def test_zip_slip_in_a_bridge_member_is_rejected_before_anything_is_stopped(self, tmp_path):
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("config", "x")
            zf.writestr(f"{fabric_backup.BRIDGE_MEMBER_PREFIX}../../escape.txt", "pwned")
            # The one-dot variant: escapes only once the prefix is stripped —
            # the raw archive-relative name still resolves inside the
            # archive root, which is exactly why the check must be done on
            # the WRITTEN (prefix-stripped) path, not the raw member name.
            zf.writestr(f"{fabric_backup.BRIDGE_MEMBER_PREFIX}../escape2.txt", "pwned too")
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log, ctl, bridge = _two_controls()

        with pytest.raises(ValueError, match="zip-slip"):
            fabric_backup.restore_backup(
                str(evil), fresh_storage, ctl, now=_NOW,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert log == []
        assert not (tmp_path / "escape.txt").exists()
        assert not (tmp_path / "escape2.txt").exists()
        assert not (tmp_path / "restored" / "escape2.txt").exists()

    def test_bridge_members_are_skipped_when_no_bridge_control_is_given(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        logger = FakeLogger()
        control = FakeControl()

        result = fabric_backup.restore_backup(archive, storage, control, now=_NOW, logger=logger)

        assert result["bridge_restored"] is False
        assert result["bridge_members"] == 3
        warning = logger.text("warning")
        assert fabric_backup.BRIDGE_MEMBER_PREFIX in warning
        assert "Stop the Matter bridge" in warning
        # C12: the reason clause names the actual cause, not just the recipe.
        assert "this restore was given no way to stop the bridge node" in warning
        assert os.path.isfile(os.path.join(storage, "config"))

    def test_a_controller_only_archive_never_touches_the_bridge_control(self, tmp_path):
        _storage, archive = self._archive(tmp_path, bridge=False)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is False
        assert not any(entry.startswith("bridge.") for entry in log)  # not even is_alive
        assert "bridge" not in logger.text("warning").lower()

    def test_a_bridge_dest_inside_the_controller_storage_is_refused_not_extracted(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        bridge_dest = os.path.join(storage, "bridge-node")  # INSIDE the controller's storage
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is False
        assert "overlaps" in logger.text("warning")
        assert not os.path.exists(os.path.join(storage, "bridge-node"))
        assert not any(entry.startswith("bridge.") for entry in log)
        assert os.path.isfile(os.path.join(storage, "config"))

    def test_a_failing_bridge_restart_during_rollback_is_loud_but_not_fatal(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        log: list[str] = []
        ctl = FakeControl(name="ctl", log=log, fail_start=True)
        bridge = FakeControl(name="bridge", log=log, start_always_false=True)
        logger = FakeLogger()

        with pytest.raises(RuntimeError, match="rolled back"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        with open(os.path.join(storage, "config")) as fh:
            assert fh.read() == "ORIGINAL-LIVE"
        err = logger.text("error")
        assert "bridge" in err.lower()
        assert "pairings are intact" in err

    def test_a_failing_bridge_move_aside_undoes_the_controller_move_and_restarts_both(self, tmp_path):
        """F1 (HIGH): move-aside must be all-or-nothing and recoverable.

        A controller rename that succeeds followed by a bridge rename that
        raises must not strand the controller fabric aside with both daemons
        stopped — the controller move is undone and both daemons come back.
        """
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        # Pre-create the bridge's collision dir WITH CONTENT (the fixed clock
        # makes the stamp deterministic) — an empty dir renames over fine on
        # some platforms, so this must be non-empty to guarantee the OSError.
        collision = f"{bridge_dest}.pre-restore-{fabric_backup._stamp(_NOW)}"
        os.makedirs(collision)
        with open(os.path.join(collision, "stray.txt"), "w") as fh:
            fh.write("leftover")
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        with pytest.raises(RuntimeError, match="moving the existing storage aside"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        # controller storage back IN PLACE with its original content — the
        # rename was undone, not left aside.
        with open(os.path.join(storage, "config")) as fh:
            assert fh.read() == "ORIGINAL-LIVE"
        parent = os.path.dirname(storage)
        stem = os.path.basename(storage)
        assert not any(name.startswith(f"{stem}.pre-restore-") for name in os.listdir(parent))
        # both daemons were restarted, best-effort, in that order.
        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered[-2:] == ["ctl.start", "bridge.start"]

    def test_a_failing_bridge_move_aside_restart_failures_are_logged(self, tmp_path):
        """Mutation guard: removing the restarts must be caught by a test."""
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        collision = f"{bridge_dest}.pre-restore-{fabric_backup._stamp(_NOW)}"
        os.makedirs(collision)
        with open(os.path.join(collision, "stray.txt"), "w") as fh:
            fh.write("leftover")
        log: list[str] = []
        ctl = FakeControl(name="ctl", log=log, start_always_false=True)
        bridge = FakeControl(name="bridge", log=log, start_always_false=True)
        logger = FakeLogger()

        with pytest.raises(RuntimeError, match="moving the existing storage aside"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        # both starts were attempted even though both returned False.
        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered[-2:] == ["ctl.start", "bridge.start"]
        err = logger.text("error")
        assert "matter-server" in err.lower()
        assert "bridge" in err.lower()

    def test_move_aside_single_fault_raising_start_is_still_reported_and_bridge_restarted(self, tmp_path):
        """F1: server_control.start() in the move-aside except handler was the
        ONE unguarded start() in the module — a raise there used to propagate
        straight past the CRITICAL log line, the bridge restart, and the
        wrapped RuntimeError. It must be caught like _rollback's start() is.
        """
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        collision = f"{bridge_dest}.pre-restore-{fabric_backup._stamp(_NOW)}"
        os.makedirs(collision)
        with open(os.path.join(collision, "stray.txt"), "w") as fh:
            fh.write("leftover")
        log: list[str] = []
        ctl = FakeControl(name="ctl", log=log, fail_start=True)  # start() raises
        bridge = FakeControl(name="bridge", log=log)
        logger = FakeLogger()

        with pytest.raises(RuntimeError, match="moving the existing storage aside"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        err = logger.text("error")
        assert "CRITICAL" in err
        assert "restart raised" in err
        # the bridge was still restarted despite the controller's start() raising.
        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered[-2:] == ["ctl.start", "bridge.start"]

    def test_move_aside_double_fault_skips_controller_start_and_names_the_aside_path(self, tmp_path, monkeypatch):
        """F2: the double fault (bridge rename AND its undo both fail) must not
        be treated like the single fault. Starting matter-server here would
        recreate storage_path as a FRESH EMPTY fabric, blocking the manual
        `mv` the error message prescribes — so the controller start must be
        SKIPPED. The bridge's own dir was never touched, so it still restarts.
        """
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        moved_aside_to = f"{storage}.pre-restore-{fabric_backup._stamp(_NOW)}"
        real_rename = fabric_backup.os.rename

        def flaky_rename(src, dst):
            if src == bridge_dest:
                raise OSError("simulated bridge rename failure")
            if src == moved_aside_to and dst == storage:
                raise OSError("simulated undo failure")
            return real_rename(src, dst)

        monkeypatch.setattr(fabric_backup.os, "rename", flaky_rename)

        with pytest.raises(RuntimeError) as excinfo:
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        msg = str(excinfo.value)
        assert moved_aside_to in msg
        assert "stop matter-server" in msg
        # the controller start must NOT have been attempted.
        assert "ctl.start" not in log
        # the bridge's own dir was never touched by this fault, so it still
        # restarts best-effort.
        assert "bridge.start" in log
        err = logger.text("error")
        assert "with matter-server stopped" in err

    # ------------------------------------------------------------------
    # C1 — R1 bridge-rollback-mechanics failure must not abort the
    # controller's rollback, and the two message shapes fire correctly.
    # ------------------------------------------------------------------
    def test_bridge_rollback_mechanics_failure_does_not_abort_controller_rollback(self, tmp_path, monkeypatch):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        real_extract = fabric_backup._safe_extract

        def flaky_extract(archive_path, dest, *, prefix=""):
            if prefix:
                raise RuntimeError("boom on bridge extract")
            return real_extract(archive_path, dest, prefix=prefix)

        monkeypatch.setattr(fabric_backup, "_safe_extract", flaky_extract)

        real_rename = fabric_backup.os.rename

        def flaky_rename(src, dst):
            # Fail ONLY the bridge rollback's rename-back (moved_aside_to ->
            # bridge.dest) — scoped by both src and dst so nothing else
            # (the controller's own renames, the bridge's failed-aside
            # rename) is affected, keeping the test order-independent.
            if os.path.basename(src).startswith("bridge-node.pre-restore-") and dst == bridge_dest:
                raise OSError("simulated rename-back failure")
            return real_rename(src, dst)

        monkeypatch.setattr(fabric_backup.os, "rename", flaky_rename)

        with pytest.raises(RuntimeError, match="rolled back"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        # the controller rollback STILL completed despite the bridge failure.
        with open(os.path.join(storage, "config")) as fh:
            assert fh.read() == "ORIGINAL-LIVE"
        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert "ctl.start" in ordered
        err = logger.text("error")
        assert "PRESERVED at" in err
        assert "move it back" in err

    # ------------------------------------------------------------------
    # C2 — a raising bridge start() after an otherwise-successful restore
    # is reported, never swallowed.
    # ------------------------------------------------------------------
    def test_bridge_restart_raise_after_success_is_reported_not_swallowed(self, tmp_path):
        _storage, archive = self._archive(tmp_path)
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        _log, ctl, bridge = _two_controls(fail_start=True)
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, fresh_storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is True
        assert result["bridge_started"] is False
        assert "did not start" in logger.text("error")
        assert "restart raised" in logger.text("debug")

    # ------------------------------------------------------------------
    # C3 — fallback (b): a bridge control WAS given, but there is no usable
    # bridge storage path.
    # ------------------------------------------------------------------
    def test_bridge_control_given_but_no_storage_path_warns_and_skips(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        logger = FakeLogger()
        control = FakeControl()
        bridge = FakeControl()

        result = fabric_backup.restore_backup(
            archive, storage, control, now=_NOW, logger=logger,
            bridge_storage_path=None, bridge_control=bridge)

        assert result["bridge_restored"] is False
        warning = logger.text("warning")
        assert "no usable bridge storage path" in warning
        assert os.path.isfile(os.path.join(storage, "config"))

    # ------------------------------------------------------------------
    # C4 — bridge-side H4: an emptied bridge extraction must refuse and
    # roll BOTH sides back, not just skip the bridge.
    # ------------------------------------------------------------------
    def test_bridge_side_empty_extraction_refuses_and_rolls_both_dirs_back(self, tmp_path, monkeypatch):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        with open(os.path.join(bridge_dest, "identity.json"), "w") as fh:
            fh.write("ORIGINAL-BRIDGE")
        log, ctl, bridge = _two_controls()

        real_extract = fabric_backup._safe_extract

        def noop_bridge_extract(archive_path, dest, *, prefix=""):
            if prefix:
                return None  # extracts nothing — the H4 empty-result case
            return real_extract(archive_path, dest, prefix=prefix)

        monkeypatch.setattr(fabric_backup, "_safe_extract", noop_bridge_extract)

        with pytest.raises(RuntimeError, match="rolled back"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        with open(os.path.join(storage, "config")) as fh:
            assert fh.read() == "ORIGINAL-LIVE"
        with open(os.path.join(bridge_dest, "identity.json")) as fh:
            assert fh.read() == "ORIGINAL-BRIDGE"
        ordered = [entry for entry in log if not entry.endswith(".is_alive")]
        assert ordered[-2:] == ["ctl.start", "bridge.start"]

    # ------------------------------------------------------------------
    # C7 — the other two overlap clauses (the third is already pinned by
    # test_a_bridge_dest_inside_the_controller_storage_is_refused_not_extracted).
    # ------------------------------------------------------------------
    def test_bridge_dest_identical_to_controller_storage_is_refused(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=storage, bridge_control=bridge)

        assert result["bridge_restored"] is False
        # C12: the reason clause names both paths, not just "overlaps".
        assert f"the bridge storage path {storage} overlaps the controller's" in logger.text("warning")
        assert not any(entry.startswith("bridge.") for entry in log)
        assert os.path.isfile(os.path.join(storage, "config"))

    def test_controller_storage_inside_the_bridge_dest_is_refused(self, tmp_path):
        storage, archive = self._archive(tmp_path)
        bridge_dest = os.path.dirname(storage)  # storage_path is INSIDE this
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        result = fabric_backup.restore_backup(
            archive, storage, ctl, now=_NOW, logger=logger,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is False
        assert "overlaps" in logger.text("warning")
        assert not any(entry.startswith("bridge.") for entry in log)
        assert os.path.isfile(os.path.join(storage, "config"))

    # ------------------------------------------------------------------
    # C8 — directory entries in the archive (not just files) extract cleanly
    # on the prefix-stripping (bridge) path.
    # ------------------------------------------------------------------
    def test_bridge_side_directory_entries_extract_cleanly(self, tmp_path):
        archive = tmp_path / "manual.zip"
        prefix = fabric_backup.BRIDGE_MEMBER_PREFIX
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("config", "controller-config")
            zf.writestr(prefix, "")           # the bare prefix dir entry itself
            zf.writestr(f"{prefix}sub/", "")  # a nested dir entry
            zf.writestr(f"{prefix}sub/file.json", '{"a": 1}')
        fresh_storage = str(tmp_path / "restored" / "matter-server")
        bridge_dest = str(tmp_path / "restored" / "bridge-node")
        _log, ctl, bridge = _two_controls()

        result = fabric_backup.restore_backup(
            str(archive), fresh_storage, ctl, now=_NOW,
            bridge_storage_path=bridge_dest, bridge_control=bridge)

        assert result["bridge_restored"] is True
        # the bare prefix entry did not become a stray file/dir named "".
        assert sorted(os.listdir(bridge_dest)) == ["sub"]
        with open(os.path.join(bridge_dest, "sub", "file.json")) as fh:
            assert fh.read() == '{"a": 1}'

    # ------------------------------------------------------------------
    # C10 — the pre-existing controller rollback-mechanics CRITICAL branch,
    # first tested here, WITH the bridge_note appended.
    # ------------------------------------------------------------------
    def test_controller_rollback_mechanics_failure_appends_the_bridge_note(self, tmp_path, monkeypatch):
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "appsupport" / "bridge-node")
        with open(os.path.join(storage, "config"), "w") as fh:
            fh.write("ORIGINAL-LIVE")
        log: list[str] = []
        ctl = FakeControl(name="ctl", log=log, fail_start=True)  # forces rollback
        bridge = FakeControl(name="bridge", log=log)
        logger = FakeLogger()

        real_rename = fabric_backup.os.rename
        pre_restore_storage = f"{storage}.pre-restore-{fabric_backup._stamp(_NOW)}"

        def flaky_rename(src, dst):
            # Fail ONLY the controller rollback's rename-back
            # (pre_restore_storage -> storage), leaving every other rename
            # (the bridge's own, the controller's move-aside-in) untouched
            # so the test is not order-fragile.
            if src == pre_restore_storage and dst == storage:
                raise OSError("simulated controller rename-back failure")
            return real_rename(src, dst)

        monkeypatch.setattr(fabric_backup.os, "rename", flaky_rename)

        with pytest.raises(OSError):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        err = logger.text("error")
        assert "CRITICAL" in err
        assert "The Matter bridge node was stopped for this restore and is still stopped." in err

    # ------------------------------------------------------------------
    # F4 — the no-previous-copy shape of _bridge_rollback_failure_message
    # had no coverage: deleting that branch left the suite green.
    # ------------------------------------------------------------------
    def test_bridge_rollback_message_when_there_was_no_previous_bridge_copy(self, tmp_path, monkeypatch):
        """A bridge dest that did NOT exist before this restore (so
        ``moved_aside_to`` is None) whose failed-aside rename during rollback
        itself fails must report "No previous bridge storage" rather than the
        other shape's false claim that a previous copy is PRESERVED.
        """
        storage, archive = self._archive(tmp_path)
        bridge_dest = str(tmp_path / "restored" / "bridge-node")  # does NOT pre-exist
        log, ctl, bridge = _two_controls()
        logger = FakeLogger()

        real_extract = fabric_backup._safe_extract

        def flaky_extract(archive_path, dest, *, prefix=""):
            if prefix:
                raise RuntimeError("boom on bridge extract")
            return real_extract(archive_path, dest, prefix=prefix)

        monkeypatch.setattr(fabric_backup, "_safe_extract", flaky_extract)

        real_rename = fabric_backup.os.rename

        def flaky_rename(src, dst):
            if src == bridge_dest and str(dst).startswith(f"{bridge_dest}.failed-"):
                raise OSError("simulated failed-aside rename failure")
            return real_rename(src, dst)

        monkeypatch.setattr(fabric_backup.os, "rename", flaky_rename)

        with pytest.raises(RuntimeError, match="rolled back"):
            fabric_backup.restore_backup(
                archive, storage, ctl, now=_NOW, logger=logger,
                bridge_storage_path=bridge_dest, bridge_control=bridge)

        err = logger.text("error")
        assert "No previous bridge storage predated this restore" in err
        assert "PRESERVED" not in err
