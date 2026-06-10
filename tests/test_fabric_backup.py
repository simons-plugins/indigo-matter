"""Fabric backup & restore (issue #26) — pure, deterministic unit tests.

Operates entirely on ``tmp_path`` with an injected ``now`` clock and a fake
``server_control`` that records stop()/start() ordering. No real launchctl, no
real matter-server, no sleeps.
"""
from __future__ import annotations

import os
import zipfile
from datetime import datetime, timezone

import pytest

import fabric_backup


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
    """

    def __init__(
        self,
        *,
        fail_start: bool = False,
        stop_returns: bool = True,
        start_returns_false: bool = False,
        rollback_start_fails: bool = False,
    ):
        self.calls: list[str] = []
        self._fail_start = fail_start
        self._stop_returns = stop_returns
        self._start_returns_false = start_returns_false
        self._rollback_start_fails = rollback_start_fails
        self._start_count = 0

    def stop(self) -> bool:
        self.calls.append("stop")
        return self._stop_returns

    def start(self) -> bool:
        self.calls.append("start")
        self._start_count += 1
        is_first = self._start_count == 1
        if is_first and self._fail_start:
            raise RuntimeError("boom on start")
        if is_first and self._start_returns_false:
            return False
        if not is_first and self._rollback_start_fails:
            return False
        return True


class FakeLogger:
    """Captures log records by level so tests can assert on escalations."""

    def __init__(self):
        self.records: dict[str, list[str]] = {"info": [], "warning": [], "error": []}

    def _record(self, level, msg, *args):
        try:
            self.records[level].append(msg % args if args else msg)
        except Exception:  # noqa: BLE001 — never let logging break a test
            self.records[level].append(str(msg))

    def info(self, msg, *args):
        self._record("info", msg, *args)

    def warning(self, msg, *args):
        self._record("warning", msg, *args)

    def error(self, msg, *args):
        self._record("error", msg, *args)

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
