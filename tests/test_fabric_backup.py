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
    """Records stop()/start() call order; optionally raises on start()."""

    def __init__(self, *, fail_start: bool = False):
        self.calls: list[str] = []
        self._fail_start = fail_start

    def stop(self) -> bool:
        self.calls.append("stop")
        return True

    def start(self) -> bool:
        self.calls.append("start")
        if self._fail_start:
            self._fail_start = False  # only the first (real) start fails; rollback start succeeds
            raise RuntimeError("boom on start")
        return True


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
