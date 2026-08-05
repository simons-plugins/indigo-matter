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

import logging
import os
import zipfile
from datetime import datetime
from typing import Any, Optional

BACKUP_PREFIX = "fabric-"
BACKUP_SUFFIX = ".zip"
#: Archive sub-path for the **bridge node's** storage dir (PRD-indigo-matter-export
#: §4.3). Losing that directory loses two different irreplaceable things: the
#: operational credentials for every ecosystem the bridge is paired into, and the
#: ``endpoint-map.json`` whose loss duplicates every exported accessory in every
#: one of them. It is a sibling of the controller's storage dir, not a child, so
#: the archive needs a reserved prefix to keep the two apart — and the prefix is
#: what makes an old (controller-only) archive still restorable: it has no
#: members under here, so the restore simply finds none.
BRIDGE_MEMBER_PREFIX = "bridge-node/"
# UTC stamp embedded in archive + move-aside names, e.g. 20260610T124145Z.
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_KEEP = 10

# Module-level fallback used when a caller does not inject one. The plugin always
# passes ``self.logger``; tests pass a fake or ``logging.getLogger``. A real logger
# matters here because the failure paths (rollback escalation, start/stop failures,
# skipped prune deletions) are precisely the moments the user must be told about.
_FALLBACK_LOGGER = logging.getLogger("fabric_backup")


def _resolve_logger(logger: Optional[Any]) -> Any:
    return logger if logger is not None else _FALLBACK_LOGGER


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


def _add_tree(zf: zipfile.ZipFile, root: str, prefix: str = "") -> int:
    """Add every file under ``root`` to ``zf`` under ``prefix``; return the count."""
    root = os.path.normpath(root)
    written = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            arcname = prefix + os.path.relpath(abs_path, root)
            zf.write(abs_path, arcname)
            written += 1
    return written


def create_backup(storage_path: str, *, now: datetime, logger: Optional[Any] = None,
                  bridge_storage_path: Optional[str] = None) -> str:
    """Zip the storage dir into ``backups/fabric-<stamp>.zip``; return its path.

    Paths inside the archive are stored relative to the storage-dir root so a
    restore is unambiguous. The backups dir is created if absent. After a
    successful write the backup set is pruned to :data:`DEFAULT_KEEP`. Raises
    ``FileNotFoundError`` if the storage dir is missing or empty — there is no
    fabric to protect, and a silent empty backup would be worse than none.

    The archive is written to ``<final>.tmp`` first, then validated (non-empty,
    at least one member, ``testzip`` clean) and atomically ``os.replace``-d into
    its final ``fabric-*.zip`` name. This is a LIVE snapshot (the server is not
    stopped) so a disk-full / file-vanished-mid-snapshot is a real risk: writing
    to a ``.tmp`` name means a partial/failed archive is never offered by
    ``list_backups``/the picker (which only match ``fabric-*.zip``), and the
    ``.tmp`` is deleted on any failure before the error is re-raised.

    ``bridge_storage_path`` adds the **export** side's storage dir under
    :data:`BRIDGE_MEMBER_PREFIX` (PRD-indigo-matter-export §4.3: "Back it up
    alongside the controller's storage"). It is optional and a missing directory
    is skipped rather than raising — a user who exports nothing has no bridge
    storage, and that must not stop the controller's fabric being protected.

    **On snapshotting the bridge dir while its node is running.** The two files
    that carry identity — ``identity.json`` and ``endpoint-map.json`` — are
    written by the node through a temp-file-plus-``rename``, so a reader either
    sees the whole previous version or the whole new one and never a torn file.
    matter.js's own storage under that dir carries the same live-snapshot caveat
    the controller's already does and gets it for the same reason: stopping the
    node to take a backup would drop every ecosystem's subscriptions, which is a
    worse and much more frequent harm than the narrow window it would close.
    """
    log = _resolve_logger(logger)
    if not os.path.isdir(storage_path):
        raise FileNotFoundError(f"Storage dir does not exist: {storage_path}")
    if not _is_nonempty_dir(storage_path):
        raise FileNotFoundError(f"Storage dir is empty, nothing to back up: {storage_path}")

    backups_dir = backups_dir_for(storage_path)
    os.makedirs(backups_dir, exist_ok=True)
    archive_path = os.path.join(backups_dir, f"{BACKUP_PREFIX}{_stamp(now)}{BACKUP_SUFFIX}")
    tmp_path = archive_path + ".tmp"

    root = os.path.normpath(storage_path)
    members_written = 0
    bridge_members = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            members_written = _add_tree(zf, root)
            if bridge_storage_path:
                if _is_nonempty_dir(bridge_storage_path):
                    bridge_members = _add_tree(zf, bridge_storage_path, BRIDGE_MEMBER_PREFIX)
                    members_written += bridge_members
                else:
                    # The promise the docstring makes ("a missing directory is
                    # skipped") only holds up if the skip is AUDIBLE. The path
                    # is derived, not configured, so the way this goes wrong in
                    # the field is that it points somewhere the node is not —
                    # and the user gets a plausible success line for an archive
                    # with no identity.json and no endpoint-map.json in it,
                    # which is exactly the archive they will reach for after
                    # losing those two files.
                    log.warning(
                        "Fabric backup: no Matter export bridge storage at %s, so this backup "
                        "does NOT contain identity.json or endpoint-map.json. That is expected "
                        "if you export nothing; if you do export devices, check where the "
                        "bridge node's --storage-path actually points.", bridge_storage_path)

        # Validate the snapshot before it can ever be offered for restore. A
        # truncated/corrupt archive that looks like a valid filename is worse
        # than no backup, because the user would trust it.
        if members_written == 0:
            raise RuntimeError(f"Backup wrote zero members from {storage_path}")
        if os.path.getsize(tmp_path) <= 0:
            raise RuntimeError(f"Backup archive is empty: {tmp_path}")
        with zipfile.ZipFile(tmp_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise RuntimeError(f"Backup archive failed integrity check (corrupt member {bad!r})")

        os.replace(tmp_path, archive_path)
    except BaseException:
        # Never leave a partial/failed archive behind — not even the .tmp.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    if bridge_members:
        # The path is named on success too: it is derived from the controller's
        # rather than configured, so "which directory did this actually cover?"
        # is a question the log should answer without anybody having to guess.
        log.info("Fabric backup written: %s (%d member(s), including %d from the Matter "
                 "export bridge node at %s)", archive_path, members_written, bridge_members,
                 bridge_storage_path)
    else:
        log.info("Fabric backup written: %s (%d member(s))", archive_path, members_written)
    prune_backups(storage_path, keep=DEFAULT_KEEP, logger=log)
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


def prune_backups(storage_path: str, keep: int = DEFAULT_KEEP, *, logger: Optional[Any] = None) -> list[str]:
    """Delete backups older than the newest ``keep``; return removed filenames."""
    log = _resolve_logger(logger)
    backups = list_backups(storage_path)
    removed: list[str] = []
    for entry in backups[keep:]:
        try:
            os.remove(entry["path"])
            removed.append(entry["filename"])
        except OSError as exc:
            # Best-effort: a backup we can't delete is not a failure of the
            # newly-written backup, so we don't propagate — but we MUST surface it
            # so a stuck-undeletable backup is visible rather than silently piling up.
            log.warning("Could not prune old fabric backup %s: %s", entry["path"], exc)
    return removed


def bridge_members_in(archive_path: str) -> list[str]:
    """Members of ``archive_path`` that belong to the bridge node's storage dir."""
    with zipfile.ZipFile(archive_path, "r") as zf:
        return [name for name in zf.namelist() if name.startswith(BRIDGE_MEMBER_PREFIX)]


def _safe_extract(archive_path: str, dest: str) -> None:
    """Extract the **controller** members of ``archive_path`` into ``dest``.

    Any member whose resolved path escapes ``dest`` (via ``..`` or an absolute
    path) is rejected before a single byte is written.

    :data:`BRIDGE_MEMBER_PREFIX` members are skipped: they belong to a different
    directory owned by a different process, and writing them under the
    controller's storage would put a second Matter node's credentials somewhere
    nothing will ever read them. :func:`restore_backup` reports them instead —
    loudly, because a half-restore nobody mentioned is how a user discovers next
    week that their exported accessories all came back as new ones.
    """
    dest_root = os.path.realpath(dest)
    with zipfile.ZipFile(archive_path, "r") as zf:
        members = [name for name in zf.namelist() if not name.startswith(BRIDGE_MEMBER_PREFIX)]
        for member in members:
            target = os.path.realpath(os.path.join(dest, member))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ValueError(f"Unsafe path in archive (zip-slip): {member!r}")
        zf.extractall(dest, members=members)


def restore_backup(
    archive_path: str,
    storage_path: str,
    server_control: Any,
    *,
    now: datetime,
    logger: Optional[Any] = None,
) -> dict:
    """Restore a fabric backup over the live storage dir — safely.

    Steps:
      1. Validate the archive exists, is a valid zip with at least one member,
         and passes ``testzip``; zip-slip guard. (All before touching anything.)
      2. ``server_control.stop()`` — the server MUST go down before we touch the
         live fabric. A False return (launchctl failed) ABORTS here, before any
         move-aside or extract, so we never write over a still-running server.
      3. Move the existing storage dir aside to
         ``<storage_path>.pre-restore-<stamp>`` (NEVER delete in place; skipped
         if the storage dir does not exist).
      4. Extract the archive into a fresh ``storage_path`` and assert the result
         is non-empty (a validly-zipped but empty backup would silently wipe the
         fabric — so that is treated as a failure and rolled back).
      5. ``server_control.start()`` — a False return is a failure (server down on
         the new fabric) and triggers rollback, same as an exception.
      6. On ANY failure during 3–5: roll back without ever stranding the original.
         If a partial new ``storage_path`` exists it is moved aside to
         ``<storage_path>.failed-<stamp>`` (not rmtree'd, so a half-removable dir
         cannot block the rename-back), THEN the original is renamed back into
         place, THEN ``start()`` is attempted on the restored fabric. A failing
         rollback ``start()`` is escalated at ERROR with explicit manual-recovery
         guidance — that is the loudest case: server down AND fabric possibly not
         back. The function always ends by raising a wrapped ``RuntimeError`` so
         the original cause is never masked.

    Returns ``{restored_from, moved_aside_to}``. ``moved_aside_to`` is ``None``
    if there was no existing storage dir to preserve.

    ``server_control`` is an abstract seam (an object with ``stop()`` and
    ``start()`` returning bools); this function never imports indigo or calls
    launchctl.

    **The bridge node's members are backed up but not restored here** (E5).
    Restoring them safely needs the bridge node stopped for the same reason step
    2 stops the controller — it holds the directory open and would write over a
    restore the moment it next persisted — and there is no bridge ``stop()``
    seam until E7 wires its launchd agent. Silently extracting them anyway would
    be the worst of the three options, so they are reported with the manual
    recipe instead, and the controller's fabric is restored as it always was.
    """
    log = _resolve_logger(logger)
    if not os.path.isfile(archive_path):
        raise FileNotFoundError(f"Backup archive does not exist: {archive_path}")
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"Not a valid zip archive: {archive_path}")
    # Surface a corrupt or content-empty zip up front (before stopping the server
    # or moving anything). A zero-member archive is a wipe waiting to happen.
    with zipfile.ZipFile(archive_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"Corrupt member in archive {archive_path}: {bad}")
        if not zf.namelist():
            raise ValueError(f"Backup archive has no members, refusing to restore (would wipe fabric): {archive_path}")

    storage_path = os.path.normpath(storage_path)
    skipped = bridge_members_in(archive_path)
    if skipped:
        log.warning(
            "This backup also contains %d file(s) from the Matter export bridge node. They are "
            "NOT being restored: the bridge node has to be stopped first and this plugin cannot "
            "stop it yet. If you need them, stop the bridge node and extract the '%s' entries of "
            "%s over %s by hand. Restoring the controller fabric only.",
            len(skipped), BRIDGE_MEMBER_PREFIX, archive_path,
            os.path.join(os.path.dirname(storage_path), "bridge-node"))

    # C1: the server MUST be down before we touch the live fabric. If stop()
    # reports failure we abort here — storage is untouched, nothing moved aside.
    if not server_control.stop():
        raise RuntimeError(
            "matter-server failed to stop; aborting restore before touching the live fabric"
        )

    moved_aside_to: str | None = None
    if os.path.isdir(storage_path):
        moved_aside_to = f"{storage_path}.pre-restore-{_stamp(now)}"
        os.rename(storage_path, moved_aside_to)

    try:
        os.makedirs(storage_path, exist_ok=True)
        _safe_extract(archive_path, storage_path)
        # H4: a validly-zipped but empty restore is a silent wipe. Refuse it.
        if not _is_nonempty_dir(storage_path):
            raise RuntimeError(
                f"Restored storage dir is empty after extracting {archive_path}; "
                "refusing to leave the fabric wiped"
            )
        # C1: start() returns a bool; False (launchctl failed) is a failure that
        # must trigger rollback exactly like an exception would.
        if not server_control.start():
            raise RuntimeError("matter-server failed to start after restore")
    except BaseException as exc:  # noqa: BLE001 — re-raised after rollback
        _rollback(storage_path, moved_aside_to, server_control, now=now, log=log)
        raise RuntimeError(
            f"Fabric restore from {archive_path} failed and was rolled back "
            f"(original fabric preserved at {moved_aside_to or storage_path})"
        ) from exc

    return {"restored_from": archive_path, "moved_aside_to": moved_aside_to}


def _rollback(
    storage_path: str,
    moved_aside_to: Optional[str],
    server_control: Any,
    *,
    now: datetime,
    log: Any,
) -> None:
    """Undo a failed restore without ever stranding or wiping the original fabric.

    Move any partial new ``storage_path`` aside (never rely on a possibly-failing
    ``rmtree`` to clear the way for the rename-back), then rename the original
    ``moved_aside_to`` back into place, then bring the server up on it. A failure
    of the rollback mechanics is escalated and re-raised; a failure of the
    rollback ``start()`` is the loudest case (user is now fabric-less / server
    down) and is logged at ERROR with manual-recovery guidance naming the
    original fabric location.
    """
    try:
        # Clear the partial new dir out of the way WITHOUT rmtree(ignore_errors):
        # a half-removable dir would otherwise leave storage_path occupied and the
        # rename-back would raise "Directory not empty", stranding the original.
        if os.path.exists(storage_path):
            failed_aside = f"{storage_path}.failed-{_stamp(now)}"
            os.rename(storage_path, failed_aside)
            log.warning("Moved partial failed restore aside to %s", failed_aside)
        if moved_aside_to is not None and os.path.isdir(moved_aside_to):
            os.rename(moved_aside_to, storage_path)
    except OSError as rollback_exc:
        # The rollback mechanics themselves failed: the original fabric is still
        # safe at moved_aside_to, but it is NOT back in place. Escalate loudly.
        log.error(
            "CRITICAL: fabric restore rollback FAILED (%s). Your original fabric is "
            "PRESERVED but NOT in place. To recover manually: stop matter-server, then "
            "move %s back to %s, then start matter-server.",
            rollback_exc, moved_aside_to, storage_path,
        )
        raise

    # Bring the server back up on the rolled-back (original) fabric.
    try:
        started = server_control.start()
    except Exception as start_exc:  # noqa: BLE001
        started = False
        log.error(
            "CRITICAL: matter-server failed to restart after restore rollback (%s).", start_exc
        )

    if not started:
        # This is the worst case the user can be in: original fabric is back on
        # disk but the server is down. Make it the loudest possible message and
        # name the original fabric location explicitly.
        log.error(
            "CRITICAL: matter-server is DOWN after restore rollback. Your original fabric "
            "has been restored to %s. To recover manually: check "
            "~/Library/Logs/indigo-matter/matter-server.err.log and start matter-server "
            "(it should pick up the original fabric at %s).",
            storage_path, storage_path,
        )
