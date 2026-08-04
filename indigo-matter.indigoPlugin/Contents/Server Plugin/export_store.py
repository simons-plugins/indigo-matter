"""The Matter export allow-list — the model behind `Manage Matter Exports…`.

Policy is fixed by ADR-0006 E2 and ``docs/PRD-indigo-matter-export.md`` §5.1:
an **explicit allow-list, default empty** (XG5). Export needs per-device
metadata beyond a boolean — role (§5.2 / BRIDGE_PROTOCOL §4.2), a display-name
override, and polarity for covering-like devices — so an entry is a small
record, not a device id.

Persistence is one JSON string in ``pluginPrefs`` under ``matterExports``
(PRD §4.3: "the plugin owns the allow-list and per-export metadata in plugin
prefs, backed up with Indigo's database"). Direct ``pluginPrefs[key] = value``
writes persist immediately — the pattern the rest of this plugin already uses.

Two disciplines worth knowing before editing:

* **The lock is re-entrant on purpose.** From E3 ``deviceUpdated`` reads the
  allow-list on Indigo's thread while the menu callbacks write it on the UI
  thread, and the public methods call each other (``upsert`` re-reads through
  ``all()`` to build the payload it persists). Same shape as
  ``device_sync.DeviceSync``'s index lock.
* **Corrupt config is preserved, never discarded.** A blob we cannot parse is
  moved aside to ``matterExports.corrupt`` and the store starts empty, so a
  bad write (or a hand-edited ``.indiPref``) costs the user a rebuild, not a
  silent, unrecoverable loss of every export they configured.

No Indigo import: the store takes a prefs-like mapping so it unit-tests
against a plain dict.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional

from bridge_protocol import ROLES

#: pluginPrefs key holding the serialised allow-list.
PREF_KEY = "matterExports"
#: pluginPrefs key a blob we could not parse is moved aside to (forensics).
PREF_KEY_CORRUPT = "matterExports.corrupt"

#: Schema version of the serialised payload. Bump when the entry shape changes.
SCHEMA_VERSION = 1

KEY_VERSION = "v"
KEY_EXPORTS = "exports"
KEY_DEVICE_ID = "indigoDeviceId"
KEY_ROLE = "role"
KEY_NAME_OVERRIDE = "nameOverride"
KEY_OPTIONS = "options"

#: ``options`` key carrying window-covering polarity (PRD §5.2 / §4.1).
OPTION_INVERT = "invert"


@dataclass(frozen=True)
class ExportEntry:
    """One allow-listed device and the metadata Indigo cannot supply.

    ``indigo_device_id`` is the identity key everywhere — in this store, in
    the bridge protocol (§4.1) and in the node's endpoint map (PRD §4.3). It
    is never re-keyed on name or list position.
    """

    indigo_device_id: int
    role: str
    name_override: Optional[str] = None
    options: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """The persisted shape (one element of ``exports``)."""
        return {
            KEY_DEVICE_ID: int(self.indigo_device_id),
            KEY_ROLE: self.role,
            KEY_NAME_OVERRIDE: self.name_override,
            KEY_OPTIONS: dict(self.options),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ExportEntry":
        """Rebuild an entry, raising ``ValueError`` on anything unusable.

        Validation is deliberately strict — an entry with an unknown role
        would be rejected by the bridge node with ``unknown_role`` (§1.1)
        long after the user could connect it to what they did.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"export entry is not an object: {raw!r}")
        try:
            device_id = int(raw[KEY_DEVICE_ID])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"export entry has no usable {KEY_DEVICE_ID}: {raw!r}") from exc
        role = raw.get(KEY_ROLE)
        if role not in ROLES:
            raise ValueError(f"export entry has unknown role {role!r} (device {device_id})")
        name_override = raw.get(KEY_NAME_OVERRIDE)
        if name_override is not None and not isinstance(name_override, str):
            raise ValueError(f"export entry name override is not a string (device {device_id})")
        options = raw.get(KEY_OPTIONS) or {}
        if not isinstance(options, dict):
            raise ValueError(f"export entry options are not an object (device {device_id})")
        return cls(
            indigo_device_id=device_id,
            role=role,
            name_override=name_override or None,
            options=dict(options),
        )

    def label_for(self, device_name: str) -> str:
        """The Bridged Device Basic Information ``NodeLabel`` for this export (§4.1)."""
        return self.name_override or device_name


class ExportStore:
    """Thread-safe CRUD over the allow-list, persisted to plugin prefs.

    ``prefs`` is any mutable mapping; in the plugin it is ``self.pluginPrefs``.
    Every mutation writes through immediately — there is no flush to forget.
    """

    def __init__(self, prefs, logger) -> None:
        self._prefs = prefs
        self._logger = logger
        # Re-entrant: public methods call one another, and E3's deviceUpdated
        # reads from Indigo's thread while the menu writes from the UI's.
        self._lock = threading.RLock()
        self._entries: dict[int, ExportEntry] = {}
        self._load()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def all(self) -> tuple[ExportEntry, ...]:
        """Every entry, ordered by device id — an immutable snapshot."""
        with self._lock:
            return tuple(self._entries[key] for key in sorted(self._entries))

    def ids(self) -> frozenset[int]:
        """The allow-listed device ids, for O(1) membership on the hot path."""
        with self._lock:
            return frozenset(self._entries)

    def get(self, device_id: int) -> Optional[ExportEntry]:
        """The entry for ``device_id``, or ``None`` if it is not exported."""
        with self._lock:
            return self._entries.get(int(device_id))

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, device_id: object) -> bool:
        try:
            key = int(device_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        with self._lock:
            return key in self._entries

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def upsert(self, entry: ExportEntry) -> ExportEntry:
        """Add ``entry`` or replace the existing one for the same device id."""
        with self._lock:
            self._entries[int(entry.indigo_device_id)] = entry
            self._save()
            return entry

    def remove(self, device_id: int) -> bool:
        """Drop ``device_id`` from the allow-list. True if it was there."""
        with self._lock:
            existed = self._entries.pop(int(device_id), None) is not None
            if existed:
                self._save()
            return existed

    def replace_all(self, entries: Iterable[ExportEntry]) -> None:
        """Replace the whole allow-list in one persisted write."""
        with self._lock:
            self._entries = {int(e.indigo_device_id): e for e in entries}
            self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _save(self) -> None:
        with self._lock:
            payload = {
                KEY_VERSION: SCHEMA_VERSION,
                KEY_EXPORTS: [entry.to_dict() for entry in self.all()],
            }
            self._prefs[PREF_KEY] = json.dumps(payload)

    def _load(self) -> None:
        raw = self._prefs.get(PREF_KEY)
        if not raw:
            self._entries = {}
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            self._corrupt(raw, f"allow-list is not valid JSON ({exc})")
            return
        if not isinstance(payload, dict):
            self._corrupt(raw, "allow-list is not a JSON object")
            return
        version = payload.get(KEY_VERSION)
        if version != SCHEMA_VERSION:
            # A future version is not ours to reinterpret, and neither is a
            # missing one — both are moved aside rather than guessed at.
            self._corrupt(raw, f"allow-list schema version {version!r} != {SCHEMA_VERSION}")
            return
        listed = payload.get(KEY_EXPORTS)
        if not isinstance(listed, list):
            self._corrupt(raw, f"allow-list {KEY_EXPORTS!r} is not a list")
            return
        entries: dict[int, ExportEntry] = {}
        dropped = 0
        for item in listed:
            try:
                entry = ExportEntry.from_dict(item)
            except ValueError as exc:
                # One bad row must not cost the user every other export, but it
                # is never silent, and the original blob is kept either way.
                dropped += 1
                self._logger.error("Matter export allow-list: dropping an unusable entry — %s", exc)
                continue
            entries[entry.indigo_device_id] = entry
        self._entries = entries
        if dropped:
            self._preserve(raw)
        self._logger.debug("Matter export allow-list loaded: %d entries (%d dropped)",
                           len(entries), dropped)

    def _corrupt(self, raw, why: str) -> None:
        """Start empty, but keep the blob — user config is never discarded."""
        self._entries = {}
        self._logger.error(
            "Matter export allow-list unreadable (%s). Starting with an EMPTY export list; "
            "the previous value is preserved in the %r plugin pref for recovery.",
            why, PREF_KEY_CORRUPT,
        )
        self._preserve(raw)

    def _preserve(self, raw) -> None:
        try:
            self._prefs[PREF_KEY_CORRUPT] = raw if isinstance(raw, str) else repr(raw)
        except Exception as exc:  # pylint: disable=broad-except
            # Preservation is best-effort: whatever the prefs mapping does, it
            # must not turn an unreadable allow-list into a failed startup.
            self._logger.exception(exc)
