"""Fabric backup & restore — protect the matter-server storage dir (issue #26).

The matter-server storage directory holds the Indigo fabric: operational
credentials for every commissioned node. It is the single point of total loss —
lose it and every Matter device must be physically factory-reset and
re-commissioned. It lives outside Indigo's database, so Indigo's own backup does
NOT protect it. The storage dir is *sacred*: this module copies it but NEVER
deletes the live fabric in place — a restore always moves the existing dir aside
first so a bad restore is itself reversible.

Everything here is pure and injectable (paths, a ``now`` ``datetime`` clock, and
an abstract ``server_control`` with ``stop()/start()``) so the whole module unit-
tests against ``tmp_path`` with a fake control — no real launchctl, no real
matter-server. The plugin passes a :class:`server_process.ServerProcess` as the
control; tests pass a fake that records the call order.

Backups live in a sibling ``backups/`` directory NEXT TO the storage dir (never
inside it), so a backup never self-includes and a restore never touches them.
"""
from __future__ import annotations

import os
import shutil
import zipfile
from datetime import datetime
from typing import Any

BACKUP_PREFIX = "fabric-"
BACKUP_SUFFIX = ".zip"
# UTC stamp embedded in archive + move-aside names, e.g. 20260610T124145Z.
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_KEEP = 10


def _stamp(now: datetime) -> str:
    """Render an injected UTC ``datetime`` as a filesystem-safe stamp."""
    return now.strftime(STAMP_FORMAT)


def backups_dir_for(storage_path: str) -> str:
    """Sibling ``backups/`` directory next to the storage dir.

    Backups live OUTSIDE the storage dir so they are never self-included by a
    backup and never touched by a restore (which only ever rewrites the storage
    dir itself).
    """
    return os.path.join(os.path.dirname(os.path.normpath(storage_path)), "backups")


def _is_nonempty_dir(path: str) -> bool:
    return os.path.isdir(path) and any(os.scandir(path))


def create_backup(storage_path: str, *, now: datetime) -> str:
    """Zip the storage dir into ``backups/fabric-<stamp>.zip``; return its path.

    Paths inside the archive are stored relative to the storage-dir root so a
    restore is unambiguous. The backups dir is created if absent. After a
    successful write the backup set is pruned to :data:`DEFAULT_KEEP`. Raises
    ``FileNotFoundError`` if the storage dir is missing or empty — there is no
    fabric to protect, and a silent empty backup would be worse than none.
    """
    if not os.path.isdir(storage_path):
        raise FileNotFoundError(f"Storage dir does not exist: {storage_path}")
    if not _is_nonempty_dir(storage_path):
        raise FileNotFoundError(f"Storage dir is empty, nothing to back up: {storage_path}")

    backups_dir = backups_dir_for(storage_path)
    os.makedirs(backups_dir, exist_ok=True)
    archive_path = os.path.join(backups_dir, f"{BACKUP_PREFIX}{_stamp(now)}{BACKUP_SUFFIX}")

    root = os.path.normpath(storage_path)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                arcname = os.path.relpath(abs_path, root)
                zf.write(abs_path, arcname)

    prune_backups(storage_path, keep=DEFAULT_KEEP)
    return archive_path


def list_backups(storage_path: str) -> list[dict]:
    """Discover ``fabric-*.zip`` backups, newest first.

    Each entry is ``{filename, path, size_bytes, mtime}`` (``mtime`` a float
    epoch). Returns ``[]`` if the backups dir does not exist yet.
    """
    backups_dir = backups_dir_for(storage_path)
    if not os.path.isdir(backups_dir):
        return []
    entries: list[dict] = []
    for name in os.listdir(backups_dir):
        if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
            continue
        path = os.path.join(backups_dir, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        entries.append({
            "filename": name,
            "path": path,
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        })
    # Newest first. The stamped filename sorts chronologically, but mtime is the
    # ground truth and breaks ties deterministically.
    entries.sort(key=lambda e: (e["mtime"], e["filename"]), reverse=True)
    return entries


def prune_backups(storage_path: str, keep: int = DEFAULT_KEEP) -> list[str]:
    """Delete backups older than the newest ``keep``; return removed filenames."""
    backups = list_backups(storage_path)
    removed: list[str] = []
    for entry in backups[keep:]:
        try:
            os.remove(entry["path"])
            removed.append(entry["filename"])
        except OSError:
            # Best-effort: a backup we can't delete is not a failure of the
            # newly-written backup, so we don't propagate.
            pass
    return removed


def _safe_extract(archive_path: str, dest: str) -> None:
    """Extract ``archive_path`` into ``dest`` with a zip-slip guard.

    Any member whose resolved path escapes ``dest`` (via ``..`` or an absolute
    path) is rejected before a single byte is written.
    """
    dest_root = os.path.realpath(dest)
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(dest, member))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ValueError(f"Unsafe path in archive (zip-slip): {member!r}")
        zf.extractall(dest)


def restore_backup(
    archive_path: str,
    storage_path: str,
    server_control: Any,
    *,
    now: datetime,
) -> dict:
    """Restore a fabric backup over the live storage dir — safely.

    Steps:
      1. Validate the archive exists and is a valid zip; zip-slip guard.
      2. ``server_control.stop()`` — the server must be down during the swap.
      3. Move the existing storage dir aside to
         ``<storage_path>.pre-restore-<stamp>`` (NEVER delete in place; skipped
         if the storage dir does not exist).
      4. Extract the archive into a fresh ``storage_path``.
      5. ``server_control.start()``.
      6. On ANY exception during 3–5: roll back (remove the partial new storage
         dir, move the ``.pre-restore-*`` copy back), then ``start()`` and
         re-raise with context. The user is never left with no fabric.

    Returns ``{restored_from, moved_aside_to}``. ``moved_aside_to`` is ``None``
    if there was no existing storage dir to preserve.

    ``server_control`` is an abstract seam (an object with ``stop()`` and
    ``start()``); this function never imports indigo or calls launchctl.
    """
    if not os.path.isfile(archive_path):
        raise FileNotFoundError(f"Backup archive does not exist: {archive_path}")
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"Not a valid zip archive: {archive_path}")
    # Surface a corrupt zip up front (before stopping the server / moving anything).
    with zipfile.ZipFile(archive_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"Corrupt member in archive {archive_path}: {bad}")

    storage_path = os.path.normpath(storage_path)
    server_control.stop()

    moved_aside_to: str | None = None
    if os.path.isdir(storage_path):
        moved_aside_to = f"{storage_path}.pre-restore-{_stamp(now)}"
        os.rename(storage_path, moved_aside_to)

    try:
        os.makedirs(storage_path, exist_ok=True)
        _safe_extract(archive_path, storage_path)
        server_control.start()
    except BaseException as exc:  # noqa: BLE001 — re-raised after rollback
        # Roll back: nuke the partial new storage dir, restore the original.
        try:
            if os.path.isdir(storage_path):
                shutil.rmtree(storage_path, ignore_errors=True)
            if moved_aside_to is not None and os.path.isdir(moved_aside_to):
                os.rename(moved_aside_to, storage_path)
        finally:
            # Always try to bring the server back up on the rolled-back fabric.
            try:
                server_control.start()
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError(
            f"Fabric restore from {archive_path} failed and was rolled back "
            f"(original fabric preserved): {exc}"
        ) from exc

    return {"restored_from": archive_path, "moved_aside_to": moved_aside_to}
