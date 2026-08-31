"""Fabric backup & restore — protect the matter-server storage dir (issue #26).

The matter-server storage directory holds the Indigo fabric: operational
credentials for every commissioned node. It is the single point of total loss —
lose it and every Matter device must be physically factory-reset and
re-commissioned. It lives outside Indigo's database, so Indigo's own backup does
NOT protect it. The storage dir is *sacred*: this module copies it but NEVER
deletes the live fabric in place — a restore always moves the existing dir aside
first so a bad restore is itself reversible.

Everything here is pure and injectable (paths, a ``now`` ``datetime`` clock, and
an abstract ``server_control`` with ``stop()/start()``, optionally ``is_alive()``)
so the whole module unit-tests against ``tmp_path`` with fake controls — no real
launchctl, no real matter-server/bridge node. The plugin passes a
:class:`server_process.ServerProcess` as ``server_control`` and a
:class:`bridge_agent.BridgeProcess` as ``bridge_control``; tests pass fakes that
record the call order.

Backups live in a sibling ``backups/`` directory NEXT TO the storage dir (never
inside it), so a backup never self-includes and a restore never touches them.
"""
from __future__ import annotations

import logging
import os
import shutil
import zipfile
from dataclasses import dataclass
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
                        "Fabric backup: no Matter bridge storage at %s, so this backup "
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
                 "bridge node at %s)", archive_path, members_written, bridge_members,
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


@dataclass
class _BridgePlan:
    """Everything a restore needs to know about the bridge half of the archive.

    Bundled rather than several separate parameters: :func:`_rollback` needs the
    same set, pylint's ``max-args`` is 8, and "is there bridge work to do at
    all" collapses to one ``is None`` check throughout :func:`restore_backup`.
    """
    dest: str                       # bridge storage dir; members extract here, prefix stripped
    control: Any                    # stop()/start() seam — never None in a live plan
    members: int
    was_alive: bool = False         # captured BEFORE the stop; gates the start (XG5)
    moved_aside_to: Optional[str] = None
    started: Optional[bool] = None  # None = we never stopped it, so we never started it


def _members_for(names: list[str], prefix: str) -> list[str]:
    """Select one side's members from an archive's full name list.

    ``prefix=""`` means the CONTROLLER's members — everything NOT under
    :data:`BRIDGE_MEMBER_PREFIX`. A truthy ``prefix`` means the bridge's own
    members, under it. The two selections are complementary by construction: no
    member is ever extracted twice or silently dropped.
    """
    if prefix:
        return [name for name in names if name.startswith(prefix) and name != prefix]
    return [name for name in names if not name.startswith(BRIDGE_MEMBER_PREFIX)]


def _assert_no_zip_slip(names: list[str], dest: str, prefix: str = "") -> list[str]:
    """Select this side's members and prove every one lands inside ``dest``.

    The path checked is the one that will actually be WRITTEN — prefix
    stripped, exactly as :func:`_safe_extract` writes it — not the raw member
    name, or a bridge member crafted as ``bridge-node/../escape.txt`` would
    look safe under the archive-relative name (it resolves inside the archive
    root) while still escaping ``dest`` once the prefix is removed. Returns
    the selected members so a caller who already has this list does not have
    to filter twice.
    """
    members = _members_for(names, prefix)
    dest_root = os.path.realpath(dest)
    for member in members:
        target = os.path.realpath(os.path.join(dest, member[len(prefix):]))
        if target != dest_root and not target.startswith(dest_root + os.sep):
            raise ValueError(f"Unsafe path in archive (zip-slip): {member!r}")
    return members


def _safe_extract(archive_path: str, dest: str, *, prefix: str = "") -> None:
    """Extract this side's members of ``archive_path`` into ``dest``.

    ``prefix=""`` (the default, and the only mode before #136) selects the
    **controller's** members — everything not under :data:`BRIDGE_MEMBER_PREFIX`
    — and extracts them at ``dest``'s root, byte-identical to before. A truthy
    ``prefix`` selects the **bridge's** own members and strips it off the
    written path, so ``bridge-node/identity.json`` in the archive lands at
    ``dest/identity.json``. :meth:`zipfile.ZipFile.extractall` cannot do that
    renaming without mutating shared ``ZipInfo`` objects, so the prefix path
    copies each member by hand instead.

    Any member whose resolved (prefix-stripped) path would escape ``dest`` is
    rejected via :func:`_assert_no_zip_slip` before a single byte is written.
    :func:`restore_backup` additionally pre-flights both sides before either
    control is stopped — the check here is defence in depth, not the only line.

    Which side gets restored at all is decided by :func:`restore_backup`
    (via :func:`_plan_bridge_restore`): a missing bridge control or storage
    path means the bridge side is reported and skipped, never extracted with a
    truthy ``prefix``.
    """
    with zipfile.ZipFile(archive_path, "r") as zf:
        members = _assert_no_zip_slip(zf.namelist(), dest, prefix)
        if not prefix:
            zf.extractall(dest, members=members)
            return
        for member in members:
            rel = member[len(prefix):]
            if rel.endswith("/"):
                os.makedirs(os.path.join(dest, rel), exist_ok=True)
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


#: The bridge-skip warning, one shared template for every reason it fires:
#: no control to stop it with, no usable storage path, or a storage path
#: that overlaps the controller's. Loud on purpose — a half-restore nobody
#: mentioned is how a user discovers next week that every exported
#: accessory came back as a new one.
_SKIP_MSG = ("This backup also contains %d file(s) from the Matter bridge node and they are NOT being "
    "restored: %s. To restore them by hand, stop the bridge node (Plugins ▸ Matter ▸ Stop the "
    "Matter bridge…), then extract the '%s' entries of %s over %s. Restoring the controller fabric only.")


def _plan_bridge_restore(
    archive_path: str,
    storage_path: str,
    bridge_storage_path: Optional[str],
    bridge_control: Optional[Any],
    *,
    log: Any,
) -> Optional[_BridgePlan]:
    """What :func:`restore_backup` needs to restore the bridge side too, or ``None``.

    Returns ``None`` SILENTLY when the archive has no bridge members at all —
    an old, controller-only archive must restore exactly as it always did,
    with nothing extra logged. Every OTHER reason to fall back to
    controller-only (no control given, no usable path, an overlapping path) is
    a real member left behind, so it is a WARNING naming the manual recipe —
    a fallback, never a refusal: the controller fabric is the single point
    of total loss, the bridge storage is recoverable by re-pairing.
    """
    members = bridge_members_in(archive_path)
    if not members:
        return None

    recipe_dest = os.path.join(os.path.dirname(storage_path), "bridge-node")
    if bridge_control is None or not bridge_storage_path:
        reason = ("this restore was given no way to stop the bridge node" if bridge_control is None
                  else "there is no usable bridge storage path")
        log.warning(_SKIP_MSG, len(members), reason, BRIDGE_MEMBER_PREFIX, archive_path, recipe_dest)
        return None

    dest = os.path.normpath(bridge_storage_path)
    if dest == storage_path or dest.startswith(storage_path + os.sep) or storage_path.startswith(dest + os.sep):
        reason = f"the bridge storage path {dest} overlaps the controller's"
        log.warning(_SKIP_MSG, len(members), reason, BRIDGE_MEMBER_PREFIX, archive_path, recipe_dest)
        return None

    return _BridgePlan(dest, bridge_control, len(members))


def _control_is_alive(control: Any, log: Any) -> bool:
    """Whether ``control``'s process is (or may be) up. Defaults to True when unsure.

    ``is_alive()`` is an OPTIONAL extension of the ``stop()``/``start()`` seam
    (:meth:`launch_agent.LaunchAgent.is_alive`) — a control without it is
    treated as alive. Fail-SAFE, and deliberately asymmetric with the rest of
    this module's failure handling: the cost of stopping an already-stopped
    node is one log line, while the cost of extracting new storage under a
    still-live node is a corrupted store.
    """
    probe = getattr(control, "is_alive", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception as exc:  # pylint: disable=broad-except
        # A raising is_alive() is not evidence of death — but it must not be
        # silent, or a persistently-raising probe looks indistinguishable
        # from one that has never been called.
        log.debug("bridge is_alive() raised, treating as alive: %s", exc)
        return True


def _restart_bridge(plan: _BridgePlan, log: Any) -> bool:
    """Start the bridge node back up. Never raises; the caller decides what a False means.

    Four different callers use this result, and every one of them checks it:
    the abort path in :func:`_stop_for_restore` (a best-effort restart when
    the controller failed to stop), the move-aside failure path in
    :func:`restore_backup`, the post-restore finish step in
    :func:`_finish_bridge_restore` (which logs a specific ERROR naming where
    the restored storage sits), and the rollback's bridge-restart step in
    :func:`_rollback` (already escalating loudly and appending to that
    message instead). Each logs its own consequence, in its own words — none
    of them wants this function logging on their behalf and either
    duplicating or contradicting it — ``log`` is only touched here if
    ``start()`` itself raises.
    """
    try:
        return bool(plan.control.start())
    except Exception as exc:  # pylint: disable=broad-except
        log.debug("Matter bridge node restart raised: %s", exc)
        return False


def _load_and_validate_archive(archive_path: str) -> list[str]:
    """Restore step 1: exists, is a valid zip, ``testzip`` clean, non-empty.

    All before touching anything — a corrupt or content-empty zip is surfaced
    up front, before either control is stopped or a byte is moved. A
    zero-member archive is a wipe waiting to happen.
    """
    if not os.path.isfile(archive_path):
        raise FileNotFoundError(f"Backup archive does not exist: {archive_path}")
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"Not a valid zip archive: {archive_path}")
    with zipfile.ZipFile(archive_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValueError(f"Corrupt member in archive {archive_path}: {bad}")
        names = zf.namelist()
        if not names:
            raise ValueError(f"Backup archive has no members, refusing to restore (would wipe fabric): {archive_path}")
    return names


def _stop_for_restore(plan: Optional[_BridgePlan], server_control: Any, log: Any) -> None:
    """Stop the bridge (if alive) then the controller; raise on either failure.

    Bridge first — cheapest abort on the failure-prone control, and the
    controller keeps its existing (pre-#136) abort semantics either way. A
    controller stop failure gets a best-effort bridge restart before raising,
    so an abort here never leaves a bridge we just stopped down for nothing.
    """
    if plan is not None:
        plan.was_alive = _control_is_alive(plan.control, log)
        if plan.was_alive and not plan.control.stop():
            raise RuntimeError(
                "the Matter bridge node failed to stop; aborting restore before touching anything"
            )
    # C1 (stop half): a False stop() is honoured as an abort. A RAISING
    # stop() must be treated the same way — otherwise a bridge we just
    # stopped above is left down with no restart attempted and no log line,
    # because the exception would propagate straight past the bridge-restart
    # logic below it.
    try:
        controller_stopped = server_control.stop()
    except Exception as stop_exc:  # pylint: disable=broad-except
        if plan is not None and plan.was_alive and not _restart_bridge(plan, log):
            log.error(
                "The Matter bridge node did not restart after the controller's stop() "
                "raised (%s). Its pairings are intact; exported accessories are "
                "unavailable until it starts — check the bridge node's error log, then "
                "reload the plugin.", stop_exc,
            )
        raise RuntimeError(
            "matter-server failed to stop; aborting restore before touching the live fabric"
        ) from stop_exc
    if not controller_stopped:
        if plan is not None and plan.was_alive and not _restart_bridge(plan, log):
            # Best-effort: put the bridge back the way we found it before
            # raising — nothing else in this function has touched anything yet.
            # A failure here is not fatal to the abort, but it must not be
            # silent — the same consequence the rollback's bridge-restart
            # step and the move-aside failure path already spell out.
            log.error(
                "The Matter bridge node did not restart after the controller failed to "
                "stop. Its pairings are intact; exported accessories are unavailable "
                "until it starts — check the bridge node's error log, then reload the "
                "plugin."
            )
        raise RuntimeError(
            "matter-server failed to stop; aborting restore before touching the live fabric"
        )


class _MoveAsideDoubleFault(OSError):
    """The bridge rename failed AND undoing the controller's rename also failed.

    Distinct from the single-fault case (a bridge rename failure whose undo
    of the controller rename succeeded, or never had to happen) so
    :func:`restore_backup`'s except handler can tell them apart: a double
    fault leaves the controller fabric stranded at ``moved_aside_to``, not at
    ``storage_path`` — starting matter-server would recreate ``storage_path``
    as a fresh, empty fabric (the launch agent's ``ensure_installed`` makes
    the dir), which then blocks the manual ``mv`` recovery this exception
    describes. Carries both paths so the handler can name them without
    re-deriving anything.
    """

    def __init__(self, moved_aside_to: str, storage_path: str):
        super().__init__(
            f"double fault moving storage aside: controller fabric stranded at "
            f"{moved_aside_to}, not in place at {storage_path}"
        )
        self.moved_aside_to = moved_aside_to
        self.storage_path = storage_path


def _move_aside_for_restore(
    storage_path: str, plan: Optional[_BridgePlan], now: datetime, log: Any
) -> Optional[str]:
    """Move existing dir(s) aside as one unit, before either extract runs.

    NEVER deletes in place. Controller then bridge, so :func:`_rollback` has
    one uniform shape to undo regardless of which side later fails.

    All-or-nothing: a controller rename that succeeds followed by a bridge
    rename that raises would otherwise strand the controller fabric aside
    with nothing to show for it, so a failing bridge rename undoes the
    controller rename before re-raising — after this function returns
    normally OR raises, either both sides are aside or neither is, never one.
    If the undo itself also raises (a double fault, vanishingly rare), it is
    raised as :class:`_MoveAsideDoubleFault` instead of the bare
    ``OSError`` — the single-fault case keeps raising the original — so the
    caller can tell the two apart and respond differently.
    """
    moved_aside_to: Optional[str] = None
    if os.path.isdir(storage_path):
        moved_aside_to = f"{storage_path}.pre-restore-{_stamp(now)}"
        os.rename(storage_path, moved_aside_to)
    if plan is not None and os.path.isdir(plan.dest):
        bridge_moved_aside_to = f"{plan.dest}.pre-restore-{_stamp(now)}"
        try:
            os.rename(plan.dest, bridge_moved_aside_to)
        except OSError:
            if moved_aside_to is not None:
                try:
                    os.rename(moved_aside_to, storage_path)
                except OSError as undo_exc:
                    log.error(
                        "Moving the Matter bridge node's storage aside failed, and undoing "
                        "the controller's move-aside ALSO failed (%s): the controller fabric "
                        "is at %s, NOT in place at %s — move it back by hand, with "
                        "matter-server stopped.",
                        undo_exc, moved_aside_to, storage_path,
                    )
                    raise _MoveAsideDoubleFault(moved_aside_to, storage_path) from undo_exc
            raise
        plan.moved_aside_to = bridge_moved_aside_to
    return moved_aside_to


def _extract_for_restore(archive_path: str, storage_path: str, plan: Optional[_BridgePlan]) -> None:
    """Extract into fresh dir(s); raise if either result is empty (H4).

    A validly-zipped but empty backup would silently wipe the fabric (or the
    bridge storage) — refusing it here is what lets the caller's ``except``
    treat it exactly like any other extraction failure and roll back.
    """
    os.makedirs(storage_path, exist_ok=True)
    _safe_extract(archive_path, storage_path)
    if not _is_nonempty_dir(storage_path):
        raise RuntimeError(
            f"Restored storage dir is empty after extracting {archive_path}; "
            "refusing to leave the fabric wiped"
        )
    if plan is not None:
        os.makedirs(plan.dest, exist_ok=True)
        _safe_extract(archive_path, plan.dest, prefix=BRIDGE_MEMBER_PREFIX)
        if not _is_nonempty_dir(plan.dest):
            raise RuntimeError(
                f"Restored Matter bridge storage dir is empty after extracting "
                f"{archive_path}; refusing to leave it wiped"
            )


def _finish_bridge_restore(plan: Optional[_BridgePlan], log: Any) -> None:
    """After a successful restore: restart a bridge that was running, report honestly.

    Called OUTSIDE :func:`restore_backup`'s try/rollback — see the XG5
    paragraph on :func:`restore_backup` itself for why a restart failure here
    never undoes a good controller restore.
    """
    if plan is None:
        return
    if plan.was_alive:
        plan.started = _restart_bridge(plan, log)
        if not plan.started:
            log.error(
                "The fabric restore SUCCEEDED, but the Matter bridge node did not start "
                "again afterwards. Its restored storage is in place at %s and its pairings "
                "are intact; exported accessories are unavailable until it starts. Check "
                "the bridge node's error log, then reload the plugin or re-export "
                "something.", plan.dest)
    else:
        log.info(
            "Matter bridge node storage restored to %s. The node was not running, so it "
            "has been left stopped — it starts again the next time something is "
            "exported.", plan.dest)


def restore_backup(
    archive_path: str,
    storage_path: str,
    server_control: Any,
    *,
    now: datetime,
    logger: Optional[Any] = None,
    bridge_storage_path: Optional[str] = None,
    bridge_control: Optional[Any] = None,
) -> dict:
    """Restore a fabric backup over the live storage dir — safely.

    Steps (controller-only when ``bridge_control``/``bridge_storage_path`` are
    absent, or the archive has no bridge members — see
    :func:`_plan_bridge_restore`):

      1. Validate the archive exists, is a valid zip with at least one member,
         and passes ``testzip``; pre-flight the zip-slip guard for BOTH sides.
         All of this happens before either control is touched.
      2. If there is bridge work to do and the bridge is alive, stop it FIRST
         (:func:`_control_is_alive`/``bridge_control.stop()``) — cheapest abort
         on the failure-prone control. A False ``stop()`` ABORTS here; nothing
         has moved.
      3. ``server_control.stop()`` — the controller MUST go down before we
         touch the live fabric. A False return ABORTS here too, after a
         best-effort restart of any bridge we just stopped.
      4. Move the existing storage dir(s) aside to ``<path>.pre-restore-
         <stamp>`` (NEVER delete in place; skipped if a dir does not exist) —
         controller then bridge, as one unit, before either extract.
         All-or-nothing: a failing bridge rename undoes the controller
         rename first (see :func:`_move_aside_for_restore`). This step's own
         failure handler then raises a wrapped ``RuntimeError`` — in the
         (near-universal) single-fault case, the disk is unchanged and a
         restart of both daemons is attempted; in the rare double fault
         (the undo also failed), the controller fabric is stranded aside and
         its restart is deliberately skipped instead (starting it would
         recreate an empty fabric at the original path). Either way this
         failure never reaches step 7's :func:`_rollback`.
      5. Extract the archive into fresh dir(s) and assert each result is
         non-empty (a validly-zipped but empty backup would silently wipe the
         fabric — treated as a failure and rolled back).
      6. ``server_control.start()`` — a False return is a failure and triggers
         rollback, same as an exception.
      7. On ANY failure during 5–6: roll back without ever stranding either
         original — bridge undone first, then the controller (verbatim of the
         controller-only behaviour), then the controller is restarted, then
         (if we stopped it) the bridge. A failing rollback ``start()`` is
         escalated at ERROR with explicit manual-recovery guidance and a
         ``CRITICAL:`` message prefix. The function always ends by raising a
         wrapped ``RuntimeError`` so the original cause is never masked.
      8. OUTSIDE the try, and NEVER rolled back: if we stopped a live bridge,
         start it again. A False here does not undo a good controller
         restore — rolling back a restored fabric to protect a secondary
         export daemon would destroy what the user came for — it is reported
         as an ERROR naming where the restored storage already sits. If the
         bridge was not running before the restore, XG5 says it stays that
         way; it starts again on the next export.

    Returns a dict: ``restored_from``, ``moved_aside_to`` (``None`` if there
    was no existing controller storage dir to preserve), ``bridge_restored``
    (bool), ``bridge_members`` (count), ``bridge_moved_aside_to`` (``None`` if
    no existing bridge storage dir, or the bridge side was not restored), and
    ``bridge_started`` (``None`` when the bridge was never stopped for this
    restore — either it was not part of the plan, or it was already stopped;
    a bool once it was).

    ``server_control``/``bridge_control`` are abstract seams (objects with
    ``stop()``/``start()`` returning bools, and an OPTIONAL ``is_alive()`` —
    see :func:`_control_is_alive`); this function never imports indigo or
    calls launchctl.

    **Why bridge-start failure never rolls the controller back (XG5).** The
    bridge's ``start()`` returning False for "never installed" is by design
    (:meth:`launch_agent.LaunchAgent.start`) — treating it as a restore
    failure would abort every restore run against a Mac that has never
    exported anything. The aliveness gate at step 2 is what keeps that honest
    in the other direction: only a bridge that was actually running before the
    restore is expected to be running after it, so a real regression there
    IS reported loudly, just not as a reason to undo the controller.
    """
    log = _resolve_logger(logger)
    names = _load_and_validate_archive(archive_path)

    storage_path = os.path.normpath(storage_path)
    plan = _plan_bridge_restore(archive_path, storage_path, bridge_storage_path, bridge_control, log=log)

    # Pre-flight the zip-slip guard for BOTH sides before either control is
    # touched. This used to fire only inside _safe_extract, after both
    # processes were already stopped — a hostile archive must never get to
    # take two daemons down before being rejected.
    _assert_no_zip_slip(names, storage_path)
    if plan is not None:
        _assert_no_zip_slip(names, plan.dest, BRIDGE_MEMBER_PREFIX)
        log.info(
            "Restoring %d file(s) of Matter bridge node storage into %s alongside the "
            "controller fabric.", plan.members, plan.dest)

    _stop_for_restore(plan, server_control, log)

    try:
        moved_aside_to = _move_aside_for_restore(storage_path, plan, now, log)
    except _MoveAsideDoubleFault as exc:
        # Double fault: the controller fabric is stranded aside, not at
        # storage_path, and nothing there predates it. Starting matter-server
        # would recreate storage_path as a FRESH EMPTY fabric (the launch
        # agent's ensure_installed makedirs), which then blocks the manual
        # `mv` this message prescribes — so, unlike the single-fault case,
        # the controller start is deliberately SKIPPED. The bridge's own
        # directory was never touched by this fault, so it is still
        # restarted best-effort if we stopped it.
        if plan is not None and plan.was_alive:
            if not _restart_bridge(plan, log):
                log.error(
                    "The Matter bridge node did not restart after a failed move-aside during "
                    "restore. Its pairings are intact; exported accessories are unavailable "
                    "until it starts — check the bridge node's error log, then reload the "
                    "plugin."
                )
        raise RuntimeError(
            f"Fabric restore from {archive_path} failed while moving the existing storage "
            f"aside, and undoing that also failed: the controller fabric is at "
            f"{exc.moved_aside_to}, NOT in place at {exc.storage_path}. matter-server was "
            "NOT restarted — starting it now would recreate an empty fabric at the original "
            f"path. To recover manually: stop matter-server if it is running, move "
            f"{exc.moved_aside_to} back to {exc.storage_path}, then start it."
        ) from exc
    except OSError as exc:
        # Single fault: best-effort, put both daemons back the way we found
        # them. This is NOT routed through _rollback — there is nothing for
        # it to undo, the move-aside itself already did (or explicitly
        # failed to, and logged why) — this only needs to bring the daemons
        # back up. A RAISING start() must be caught here too, or the CRITICAL
        # log line below and the bridge restart never run and the wrapped
        # RuntimeError never raises — the caller would see the bare
        # exception with none of this context.
        try:
            started = server_control.start()
        except Exception as start_exc:  # pylint: disable=broad-except
            started = False
            log.error("CRITICAL: matter-server restart raised after a failed move-aside (%s)", start_exc)
        if not started:
            log.error(
                "CRITICAL: matter-server is DOWN after a failed move-aside during restore. "
                "To recover manually: check ~/Library/Logs/indigo-matter/matter-server.err.log "
                "and start matter-server (it should pick up the original fabric at %s).",
                storage_path,
            )
        if plan is not None and plan.was_alive:
            if not _restart_bridge(plan, log):
                log.error(
                    "The Matter bridge node did not restart after a failed move-aside during "
                    "restore. Its pairings are intact; exported accessories are unavailable "
                    "until it starts — check the bridge node's error log, then reload the "
                    "plugin."
                )
        raise RuntimeError(
            f"Fabric restore from {archive_path} failed while moving the existing storage "
            "aside; the disk is unchanged unless the error above says otherwise — a restart "
            "of both daemons was attempted — check the log above for restart failures"
        ) from exc

    try:
        _extract_for_restore(archive_path, storage_path, plan)
        # C1 (start half): start() returns a bool; False (launchctl failed) is
        # a failure that must trigger rollback exactly like an exception would.
        if not server_control.start():
            raise RuntimeError("matter-server failed to start after restore")
    except BaseException as exc:  # re-raised after rollback
        _rollback(storage_path, moved_aside_to, server_control, now=now, log=log, bridge=plan)
        raise RuntimeError(
            f"Fabric restore from {archive_path} failed and was rolled back "
            f"(original fabric preserved at {moved_aside_to or storage_path})"
        ) from exc

    # OUTSIDE the try: a bridge that fails to restart never rolls a good
    # controller restore back — see the docstring's XG5 paragraph.
    _finish_bridge_restore(plan, log)

    return {
        "restored_from": archive_path,
        "moved_aside_to": moved_aside_to,
        "bridge_restored": plan is not None,
        # The count of bridge members the ARCHIVE carries, not how many were
        # actually restored — a skipped/refused bridge side still tells the
        # caller how much was left behind, which is what makes the log
        # warning's count and this field agree.
        "bridge_members": plan.members if plan is not None else len(bridge_members_in(archive_path)),
        "bridge_moved_aside_to": plan.moved_aside_to if plan is not None else None,
        "bridge_started": plan.started if plan is not None else None,
    }


def _bridge_rollback_failure_message(bridge: _BridgePlan, exc: OSError) -> tuple[str, tuple]:
    """The bridge-rollback-mechanics failure text: two shapes, one cause.

    A previous bridge copy exists and could not be put back (the wording
    that shape has always had), or there was never one to put back —
    claiming a "previous copy" in that case would send a user hunting for
    something that never existed. Returns ``(message, args)`` so the caller
    logs it in one place.
    """
    if bridge.moved_aside_to is not None:
        return (
            "Rolling the Matter bridge node's storage back FAILED (%s). Its previous "
            "storage is PRESERVED at %s but is NOT in place; move it back to %s by "
            "hand with the bridge node stopped.",
            (exc, bridge.moved_aside_to, bridge.dest),
        )
    return (
        "The partially-restored Matter bridge storage at %s could not be moved "
        "aside (%s). No previous bridge storage predated this restore; clean up "
        "%s by hand with the bridge node stopped.",
        (bridge.dest, exc, bridge.dest),
    )


def _rollback(
    storage_path: str,
    moved_aside_to: Optional[str],
    server_control: Any,
    *,
    now: datetime,
    log: Any,
    bridge: Optional[_BridgePlan] = None,
) -> None:
    """Undo a failed restore without ever stranding or wiping either original.

    Bridge undone FIRST, controller second — the reverse of the extract
    order (and the same bridge-first order the stops used), so the
    controller (the single point of total loss) is always the last,
    most-supervised step. Each side moves any partial new dir aside
    (never relies on a possibly-failing ``rmtree`` to clear the way for the
    rename-back), then renames the original ``*_aside_to`` back into place.

    The bridge's own undo is wrapped in its own ``try``/``except OSError`` and
    NEVER raises — a failure to roll the bridge back must not abort the
    controller's rollback, which is the one that protects the fabric. A
    failure of the controller's rollback mechanics IS re-raised (today's
    behaviour, unchanged) and is the loudest case: escalated at ERROR with a
    ``CRITICAL:`` message prefix and manual-recovery guidance, extended with
    a note that the bridge is still stopped when this restore was the one
    that stopped it.

    Bringing the controller back up is attempted regardless (verbatim of the
    controller-only behaviour); a failure there is the second-loudest case —
    server down AND fabric possibly not back — logged the same way, at ERROR
    with a ``CRITICAL:`` prefix. Only once all of that is settled is the
    bridge restarted, if we are the one who stopped it — the rollback's
    bridge-restart step, least critical of the four, because pairings are
    intact either way and this only delays exported accessories coming back.
    """
    if bridge is not None:
        try:
            if os.path.exists(bridge.dest):
                bridge_failed_aside = f"{bridge.dest}.failed-{_stamp(now)}"
                os.rename(bridge.dest, bridge_failed_aside)
                log.warning("Moved partial failed Matter bridge restore aside to %s", bridge_failed_aside)
            if bridge.moved_aside_to is not None and os.path.isdir(bridge.moved_aside_to):
                os.rename(bridge.moved_aside_to, bridge.dest)
        except OSError as bridge_rollback_exc:
            message, args = _bridge_rollback_failure_message(bridge, bridge_rollback_exc)
            log.error(message, *args)
            # Never raise: must not abort the controller's rollback below.

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
        bridge_note = (" The Matter bridge node was stopped for this restore and is still stopped."
                       if bridge is not None and bridge.was_alive else "")
        # The rollback mechanics themselves failed: the original fabric is still
        # safe at moved_aside_to, but it is NOT back in place. Escalate loudly.
        log.error(
            "CRITICAL: fabric restore rollback FAILED (%s). Your original fabric is "
            "PRESERVED but NOT in place. To recover manually: stop matter-server, then "
            "move %s back to %s, then start matter-server.%s",
            rollback_exc, moved_aside_to, storage_path, bridge_note,
        )
        raise

    # Bring the server back up on the rolled-back (original) fabric.
    try:
        started = server_control.start()
    except Exception as start_exc:  # pylint: disable=broad-except
        # Absorbing: a raising server_control.start() after the rollback has
        # already succeeded. It must be caught for the same reason the sibling
        # at the restore leg is: the CRITICAL manual-recovery message below,
        # and the bridge restart with it, are what the user actually needs —
        # a bare traceback out of here would hide both. The raise is not
        # swallowed: it is named in the CRITICAL logged here, and `started =
        # False` then fires the loudest recovery message too, so this path
        # produces two CRITICALs and never silence.
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

    # The rollback's bridge-restart step: the bridge, if we are the one who
    # stopped it. Loud but not fatal — ERROR, not CRITICAL, because pairings
    # are intact either way.
    if bridge is not None and bridge.was_alive:
        if not _restart_bridge(bridge, log):
            log.error(
                "The Matter bridge node did not restart after the rollback. Its pairings are "
                "intact; exported accessories are unavailable until it starts — check the "
                "bridge node's error log, then reload the plugin."
            )
