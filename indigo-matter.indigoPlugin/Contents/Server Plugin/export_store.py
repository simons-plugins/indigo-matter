"""The Matter export allow-list — the model behind `Manage Matter Exports…`.

Policy is fixed by ADR-0006 E2 and ``docs/PRD-indigo-matter-export.md`` §5.1:
an **explicit allow-list, default empty** (XG5). Export needs per-device
metadata beyond a boolean — role (§5.2 / BRIDGE_PROTOCOL §4.2), a display-name
override, and polarity for covering-like devices — so an entry is a small
record, not a device id.

Persistence is one JSON string in ``pluginPrefs`` under ``matterExports``
(PRD §4.3: "the plugin owns the allow-list and per-export metadata in plugin
prefs, backed up with Indigo's database").

Four disciplines worth knowing before editing:

* **The lock is re-entrant on purpose.** From E3 ``deviceUpdated`` reads the
  allow-list on Indigo's thread while the menu callbacks write it on the UI
  thread, and the public methods call each other. Same shape as
  ``device_sync.DeviceSync``'s index lock.
* **Persist first, commit second.** :meth:`ExportStore._commit` builds the
  payload, writes it to prefs, *flushes* through the injected ``save_prefs``
  callable, and only then adopts the new mapping in memory — rolling the prefs
  key back if the flush raises. Mutating memory first (the pre-#122 shape) let
  a failed save leave the two out of step: a removed device reappeared on the
  next restart while the dialog swore it was gone.
* **Prefs are resolved late, every time.** The store holds a ``prefs_getter``
  callable, not the mapping object. Indigo may rebind ``self.pluginPrefs`` when
  the user saves a PluginConfig dialog, and a store holding the old object
  would write to an orphan nobody ever persists.
* **Corrupt config is preserved, never discarded — and the first rescue wins.**
  A blob we cannot parse is moved aside to ``matterExports.corrupt`` and the
  store starts empty, so a bad write (or a hand-edited ``.indiPref``) costs the
  user a rebuild, not a silent loss of every export they configured. A *second*
  corruption never overwrites the first rescue copy: the oldest surviving blob
  is the one most likely to still hold the user's real list. The failure is
  also carried in :attr:`ExportStore.load_error` so the dialog can say so
  rather than cheerfully reporting "nothing is exported yet".

No Indigo import: the store takes a prefs-getter and an optional entry
validator so it unit-tests against a plain dict.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import ct_bounds
import export_catalog
from bridge_protocol import ROLES, parse_published_id

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
#: Issues #219/#240 — the accessory identity this device publishes as (§4.1).
#: Additive: SCHEMA_VERSION stays 1 (see :class:`ExportEntry`).
KEY_PUBLISHED_AS = "publishedAs"

#: ``options`` key carrying window-covering polarity (PRD §5.2 / §4.1).
OPTION_INVERT = "invert"

#: ``options`` keys carrying a custom-state mapping (ADR-0012, issue #252).
#: :data:`OPTION_STATE_KEY` names the device state the export reads;
#: :data:`OPTION_STATE_INVERT` says the device reports the opposite sense (a
#: zone whose boolean is true when *healthy*, for instance). Deliberately not
#: folded into :data:`OPTION_INVERT`: that one is window-covering position
#: polarity, validated against :data:`INVERTIBLE_ROLES`, and sharing a key
#: would make each one's validation lie about the other.
OPTION_STATE_KEY = export_catalog.OPTION_STATE_KEY
OPTION_STATE_INVERT = export_catalog.OPTION_STATE_INVERT

#: Roles for which :data:`OPTION_STATE_KEY` means anything — the binary sensor
#: family. A mapping on a numeric or a light role is a hand-edit or a stale
#: write, and honouring it would read a boolean into a role with no boolean.
MAPPABLE_ROLES = tuple(export_catalog.BINARY_SENSOR_ROLES)

#: Roles for which :data:`OPTION_INVERT` means anything. Polarity is a covering
#: concept (§5.2); on any other role it is either a hand-edit or a stale write,
#: and honouring it would silently invert a lock or a plug.
INVERTIBLE_ROLES = ("windowCovering",)

#: ``options`` keys carrying physical colour-temperature bounds (issue #293).
#: ``OPTION_CT_MIN/MAX_MIREDS`` is the user's SEED from the export dialog;
#: ``OPTION_CT_LEARNED_MIN/MAX_MIREDS`` is the observed-clamp learner's own
#: record — see ``ct_bounds.py``, which owns all four names and the roles
#: they mean anything on, for the reasoning.
OPTION_CT_MIN_MIREDS = ct_bounds.OPTION_CT_MIN_MIREDS
OPTION_CT_MAX_MIREDS = ct_bounds.OPTION_CT_MAX_MIREDS
OPTION_CT_LEARNED_MIN_MIREDS = ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS
OPTION_CT_LEARNED_MAX_MIREDS = ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS

#: Roles for which the four CT-bounds keys above mean anything. A colour-
#: temperature bound on any other role is either a hand-edit or a stale
#: write, and honouring it would publish a physical range for a role with no
#: colour temperature to bound.
CT_BOUNDS_ROLES = tuple(ct_bounds.CT_ROLES)

#: The message the dialog shows when the whole blob was unreadable (S3). The
#: store must never let the UI say "nothing is exported yet" after this.
LOAD_ERROR_UNREADABLE = ("Export list could not be read — starting empty. "
                         "The previous list is preserved (see Event Log).")


def _validate_options(options: dict, role: str, device_id: int) -> None:
    """Raise ``ValueError`` for any option this role has no business carrying.

    Split out of :meth:`ExportEntry.from_dict` when the mapping options
    arrived. Every check here is enforced per KEY **and** per ROLE, because a
    restored backup or a hand-edited ``.indiPref`` is the one write path the
    dialog's own guards never see: an ``invert`` that rode in on a lock would
    be a silent polarity flip, and a ``stateKey`` on a numeric role would have
    the export read a boolean into a role that publishes none.
    """
    if OPTION_INVERT in options:
        if not isinstance(options[OPTION_INVERT], bool):
            raise ValueError(
                f"export entry {OPTION_INVERT!r} option is not a boolean (device {device_id})")
        if role not in INVERTIBLE_ROLES:
            raise ValueError(
                f"export entry has the {OPTION_INVERT!r} option on role {role!r}, which has "
                f"no polarity (device {device_id})")
    if OPTION_STATE_KEY in options:
        if not isinstance(options[OPTION_STATE_KEY], str) or not options[OPTION_STATE_KEY]:
            raise ValueError(
                f"export entry {OPTION_STATE_KEY!r} option is not a non-empty string "
                f"(device {device_id})")
        if role not in MAPPABLE_ROLES:
            raise ValueError(
                f"export entry has the {OPTION_STATE_KEY!r} option on role {role!r}, which "
                f"reads no boolean state (device {device_id})")
    if OPTION_STATE_INVERT in options:
        if not isinstance(options[OPTION_STATE_INVERT], bool):
            raise ValueError(
                f"export entry {OPTION_STATE_INVERT!r} option is not a boolean "
                f"(device {device_id})")
        if role not in MAPPABLE_ROLES:
            # Polarity belongs to the binary sensors and nothing else. It used
            # to require a state key as well — "an inversion with nothing to
            # invert" — which was wrong: a device with a perfectly good
            # `onState` can still report it the other way round from Matter's
            # reading, and refusing the option left a Z-Wave door sensor
            # reporting every closed door as open with no way to correct it.
            raise ValueError(
                f"export entry has the {OPTION_STATE_INVERT!r} option on role {role!r}, "
                f"which reads no boolean state (device {device_id})")
    _validate_ct_bounds(options, role, device_id)


def _validate_ct_bounds(options: dict, role: str, device_id: int) -> None:
    """Raise ``ValueError`` for the four issue #293 colour-temperature-bounds keys.

    Two independent pairs, each with its own completeness rule:

    * ``ctMinMireds``/``ctMaxMireds`` (the user's SEED) — an INCOMPLETE pair
      is itself a violation. ``export_dialog_mixin.exportAddOrUpdate`` always
      writes both keys or neither, so a lone one can only be a hand-edit or a
      partial write, and honouring it would seed just one side of a range
      nobody actually declared.
    * ``ctLearnedMinMireds``/``ctLearnedMaxMireds`` (the learner's own
      record) — a LONE key is usable on its own: the learner adopts one side
      at a time (``ct_learner.py``), and a device that has only ever proven
      its warm limit legitimately has nothing to say about the cool one yet.

    Whichever pair IS complete must satisfy §4.2's declared domain,
    ``153 <= min < max <= 500`` — a stored range wider than the fabric's own
    domain, inverted, or collapsed to a point is unusable by construction.
    """
    for min_key, max_key, seed_pair in (
        (OPTION_CT_MIN_MIREDS, OPTION_CT_MAX_MIREDS, True),
        (OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_LEARNED_MAX_MIREDS, False),
    ):
        has_min = min_key in options
        has_max = max_key in options
        if not (has_min or has_max):
            continue
        if role not in CT_BOUNDS_ROLES:
            raise ValueError(
                f"export entry has a {min_key!r}/{max_key!r} option on role {role!r}, which "
                f"has no colour temperature (device {device_id})")
        for key in (min_key, max_key):
            if key in options and not (isinstance(options[key], int)
                                        and not isinstance(options[key], bool)):
                raise ValueError(
                    f"export entry {key!r} option is not an integer (device {device_id})")
        if has_min != has_max:
            if seed_pair:
                raise ValueError(
                    f"export entry has an incomplete {min_key!r}/{max_key!r} pair — both or "
                    f"neither are required (device {device_id})")
            # A lone learned side is usable on its own, but it still has to
            # sit inside §4.2's declared domain: the complete-pair inequality
            # below never sees it, and without this a hand-edited
            # ``ctLearnedMaxMireds: 9999`` would load cleanly here and only
            # be caught by the NODE's own validity warn — leaving the plugin
            # believing a bound the fabric never adopted.
            lone_key = min_key if has_min else max_key
            lone_value = options[lone_key]
            if not ct_bounds.GENERIC_MIN_MIREDS <= lone_value <= ct_bounds.GENERIC_MAX_MIREDS:
                raise ValueError(
                    f"export entry {lone_key!r} option ({lone_value}) is outside the "
                    f"{ct_bounds.GENERIC_MIN_MIREDS}-{ct_bounds.GENERIC_MAX_MIREDS} mired "
                    f"domain (device {device_id})")
            continue  # a lone learned side is usable on its own
        min_value, max_value = options[min_key], options[max_key]
        if not (ct_bounds.GENERIC_MIN_MIREDS <= min_value < max_value <= ct_bounds.GENERIC_MAX_MIREDS):
            raise ValueError(
                f"export entry {min_key!r}/{max_key!r} pair ({min_value}, {max_value}) does not "
                f"satisfy {ct_bounds.GENERIC_MIN_MIREDS} <= min < max <= "
                f"{ct_bounds.GENERIC_MAX_MIREDS} (device {device_id})")


def options_lawful_for_role(options: dict, role: str) -> dict:
    """The subset of ``options`` that :func:`_validate_options` accepts for ``role``.

    Driven by the SAME per-role tables ``_validate_options`` checks against —
    :data:`INVERTIBLE_ROLES`, :data:`MAPPABLE_ROLES`, :data:`CT_BOUNDS_ROLES` —
    rather than a hand-copied key list that can drift out of step with them.

    For ``server_menu_mixin._readopt_commit`` (issue #293 review): re-adopting
    an orphaned accessory onto a device carries that device's OWN previous
    export options forward, and the orphan's role need not match the role
    those options were validated against (``export_catalog``'s dimmer rule
    makes one device eligible for both a light role and ``windowCovering``,
    for instance). Filtering here before the entry is built keeps
    ``ExportStore.upsert``'s options validation from hard-failing the
    re-adopt outright. Dropping the unlawful keys is correct, not a
    compromise: they described the OLD role's semantics, not this one's, and
    the learner/dialog re-earn or re-seed whatever the new role needs.
    """
    lawful = {}
    if OPTION_INVERT in options and role in INVERTIBLE_ROLES:
        lawful[OPTION_INVERT] = options[OPTION_INVERT]
    if role in MAPPABLE_ROLES:
        for key in (OPTION_STATE_KEY, OPTION_STATE_INVERT):
            if key in options:
                lawful[key] = options[key]
    if role in CT_BOUNDS_ROLES:
        for key in (OPTION_CT_MIN_MIREDS, OPTION_CT_MAX_MIREDS,
                    OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_LEARNED_MAX_MIREDS):
            if key in options:
                lawful[key] = options[key]
    return lawful


@dataclass(frozen=True)
class ExportEntry:
    """One allow-listed device and the metadata Indigo cannot supply.

    ``indigo_device_id`` is this store's key, and the device the accessory is
    DRIVEN by — never re-keyed on name or list position. It is no longer the
    accessory's identity (ADR-0010, issues #219/#240): ``published_as`` is,
    both on the wire (§4.1 ``publishedAs``) and in the node's endpoint map
    (PRD §4.3), and it defaults to ``indigo-<indigo_device_id>``. The two
    agree for every ordinary export and deliberately disagree for a re-adopted
    one, which is the whole point of separating them.
    """

    indigo_device_id: int
    role: str
    name_override: Optional[str] = None
    options: dict = field(default_factory=dict)
    #: Issues #219/#240 — the accessory identity this device publishes as
    #: (``bridge_protocol.published_id_for``/``parse_published_id``). ``None``
    #: means "use today's default derivation" — every entry written before
    #: this field existed, and every entry an update has not role-changed.
    #: Additive and optional (SCHEMA_VERSION stays 1): a payload written by an
    #: older plugin has no key at all, and ``from_dict`` already tolerates a
    #: missing key the way every other optional field here does.
    published_as: Optional[str] = None

    def to_dict(self) -> dict:
        """The persisted shape (one element of ``exports``)."""
        return {
            KEY_DEVICE_ID: int(self.indigo_device_id),
            KEY_ROLE: self.role,
            KEY_NAME_OVERRIDE: self.name_override,
            KEY_OPTIONS: dict(self.options),
            KEY_PUBLISHED_AS: self.published_as,
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
        _validate_options(options, role, device_id)
        published_as = raw.get(KEY_PUBLISHED_AS)
        if published_as is not None:
            if not isinstance(published_as, str):
                raise ValueError(
                    f"export entry {KEY_PUBLISHED_AS!r} is not a string (device {device_id})")
            # Lawfulness is the ONLY check: the identity deliberately need
            # NOT embed this entry's own device id. A re-adopt (issue #219) is
            # exactly `indigo-<OLD device>` driven by a NEW one, so demanding
            # the two agree would drop every re-adopted export the next time
            # the allow-list was loaded — silently un-exporting the accessory
            # the re-adopt existed to keep.
            if parse_published_id(published_as) is None:
                raise ValueError(
                    f"export entry {KEY_PUBLISHED_AS!r} {published_as!r} is not a lawful "
                    f"published identity (device {device_id})")
        return cls(
            indigo_device_id=device_id,
            role=role,
            name_override=name_override or None,
            options=dict(options),
            published_as=published_as,
        )

    def label_for(self, device_name: str) -> str:
        """The Bridged Device Basic Information ``NodeLabel`` for this export (§4.1)."""
        return self.name_override or device_name


class ExportStore:
    """Thread-safe CRUD over the allow-list, persisted to plugin prefs.

    :param prefs_getter: callable returning the *current* prefs mapping — in
        the plugin ``lambda: self.pluginPrefs``. A callable, not the mapping,
        because Indigo may rebind ``pluginPrefs`` on a PluginConfig save.
    :param logger: the plugin logger.
    :param save_prefs: callable that flushes prefs to Indigo's database (in the
        plugin, ``indigo.server.savePluginPrefs``). Defaults to a no-op so the
        store unit-tests against a plain dict.
    :param entry_validator: optional callable taking an :class:`ExportEntry`
        loaded from prefs and returning a rejection reason (or ``None`` to
        accept). Load is the one write path the dialog's guards do not cover —
        a restored or hand-edited blob can name a device the loop guard would
        refuse — so the plugin injects that check here.
    """

    def __init__(self, prefs_getter: Callable[[], object], logger,
                 save_prefs: Optional[Callable[[], None]] = None,
                 entry_validator: Optional[Callable[[ExportEntry], Optional[str]]] = None) -> None:
        self._prefs_getter = prefs_getter
        self._logger = logger
        self._save_prefs = save_prefs
        self._entry_validator = entry_validator
        # Re-entrant: public methods call one another, and E3's deviceUpdated
        # reads from Indigo's thread while the menu writes from the UI's.
        self._lock = threading.RLock()
        self._entries: dict[int, ExportEntry] = {}
        #: Human-readable reason the last load did not produce a faithful list,
        #: or ``None``. The dialog shows it instead of claiming an empty list
        #: is an intentionally empty one (S3).
        self.load_error: Optional[str] = None
        self._load()

    @property
    def _prefs(self):
        """The prefs mapping as it is *right now* — never a captured object."""
        return self._prefs_getter()

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
        """Add ``entry`` or replace the existing one for the same device id.

        Raises whatever the prefs write or flush raised, having changed
        nothing — see :meth:`_commit`.

        ``published_as`` is validated HERE as well as in :meth:`from_dict`
        (issues #219/#240, PR5 design E12's plugin-side half). Load-time
        validation alone means an unlawful identity is written to the prefs,
        sent to the node on the very next attach — which refuses the WHOLE
        attach with ``malformed_args``, taking every export offline — and only
        discovered on the reload after that, by which point the dialog that
        wrote it has long since reported success. Raising at the write is what
        lets the caller say "FAILED to save the export list" instead.

        ``options`` is validated HERE too (issue #294 review), for the exact
        same reason ``published_as`` already is: :meth:`ExportEntry.from_dict`
        is the LOAD path's guard, and every ordinary write goes through
        already-validated callers (the export dialog, ``ct_learner._adopt``'s
        own re-read-then-merge). But the 2026-08-24 15:39 incident proved a
        caller CAN construct an invalid pair despite its own guard racing a
        concurrent write (see ``ct_learner.CTBoundsLearner._adopt``'s
        docstring for the full mechanism) — and without a check here, that
        invalid blob is persisted silently and only rejected as corrupt at
        the NEXT plugin restart's load, by which point the evidence of what
        went wrong is long gone. Raising here instead gives the caller (in
        practice, ``_adopt``'s own try/except) an immediate, attributable
        failure to log — "could not be saved" — rather than a delayed,
        unexplained one.
        """
        if entry.published_as is not None and parse_published_id(entry.published_as) is None:
            raise ValueError(
                f"export entry {KEY_PUBLISHED_AS!r} {entry.published_as!r} is not a lawful "
                f"published identity (device {entry.indigo_device_id})")
        _validate_options(entry.options, entry.role, entry.indigo_device_id)
        with self._lock:
            pending = dict(self._entries)
            pending[int(entry.indigo_device_id)] = entry
            self._commit(pending)
            return entry

    def remove(self, device_id: int) -> bool:
        """Drop ``device_id`` from the allow-list. True if it was there."""
        with self._lock:
            key = int(device_id)
            if key not in self._entries:
                return False
            pending = dict(self._entries)
            del pending[key]
            self._commit(pending)
            return True

    def replace_all(self, entries: Iterable[ExportEntry]) -> None:
        """Replace the whole allow-list in one persisted write.

        Validated the same way :meth:`upsert` is (issue #294 review) and for
        the same reason: ``_migrate_commit`` (``server_menu_mixin.py``) is
        this method's one production caller, and it already wraps the call
        in a try/except reporting "FAILED to save the export list" — exactly
        the failure mode a raised ``ValueError`` here produces. Validated
        BEFORE ``_commit`` runs, over every entry, so one bad entry among many
        refuses the whole replacement rather than partially persisting it —
        matching :meth:`upsert`'s own "changed nothing" promise on failure.
        """
        materialized = list(entries)
        for entry in materialized:
            if entry.published_as is not None and parse_published_id(entry.published_as) is None:
                raise ValueError(
                    f"export entry {KEY_PUBLISHED_AS!r} {entry.published_as!r} is not a lawful "
                    f"published identity (device {entry.indigo_device_id})")
            _validate_options(entry.options, entry.role, entry.indigo_device_id)
        with self._lock:
            self._commit({int(e.indigo_device_id): e for e in materialized})

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _commit(self, pending: dict[int, ExportEntry]) -> None:
        """Persist ``pending``, flush, and only then adopt it in memory.

        The order is the whole point. Writing the pref, flushing it through
        Indigo, and *then* replacing ``self._entries`` means a failure at any
        step leaves memory and prefs saying the same (old) thing. The reverse
        order resurrects removed devices on the next restart while the dialog
        reports success.
        """
        with self._lock:
            payload = {
                KEY_VERSION: SCHEMA_VERSION,
                KEY_EXPORTS: [pending[key].to_dict() for key in sorted(pending)],
            }
            blob = json.dumps(payload)
            prefs = self._prefs
            had_previous = PREF_KEY in prefs
            previous = prefs.get(PREF_KEY)
            prefs[PREF_KEY] = blob
            try:
                if self._save_prefs is not None:
                    self._save_prefs()
            except Exception:
                # Put the pref back the way we found it: a half-written key the
                # in-memory list disagrees with is worse than a failed write.
                try:
                    if had_previous:
                        prefs[PREF_KEY] = previous
                    else:
                        del prefs[PREF_KEY]
                except Exception as rollback_exc:  # pylint: disable=broad-except
                    self._logger.exception(rollback_exc)
                raise
            self._entries = pending

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
            rejection = self._reject(entry)
            if rejection:
                dropped += 1
                self._logger.error(
                    "Matter export allow-list: dropping the entry for device %s — %s",
                    entry.indigo_device_id, rejection)
                continue
            entries[entry.indigo_device_id] = entry
        self._entries = entries
        if dropped:
            self.load_error = (
                f"{dropped} saved export(s) could not be read and were dropped. "
                "The previous list is preserved (see Event Log).")
            self._preserve(raw)
        self._logger.debug("Matter export allow-list loaded: %d entries (%d dropped)",
                           len(entries), dropped)

    def _reject(self, entry: ExportEntry) -> Optional[str]:
        """The injected validator's verdict on a restored entry, fail-safe.

        Load is an unguarded write path: the dialog's loop guard never sees a
        blob restored from a backup or edited by hand. A validator that itself
        blows up must not take the whole allow-list down with it, so its own
        failure is logged and the entry kept — the E3 endpoint build re-checks.
        """
        if self._entry_validator is None:
            return None
        try:
            return self._entry_validator(entry)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception(exc)
            return None

    def _corrupt(self, raw, why: str) -> None:
        """Start empty, but keep the blob — user config is never discarded."""
        self._entries = {}
        self.load_error = LOAD_ERROR_UNREADABLE
        self._logger.error(
            "Matter export allow-list unreadable (%s). Starting with an EMPTY export list; "
            "the previous value is preserved in the %r plugin pref for recovery.",
            why, PREF_KEY_CORRUPT,
        )
        self._preserve(raw)

    def _preserve(self, raw) -> None:
        """Move the unreadable blob aside — but never over an earlier rescue.

        First rescue wins. A second corruption is usually a *derivative* of the
        first (the user restarted, we wrote an empty list, that got mangled
        too); overwriting would trade the blob that still holds twenty real
        exports for one that holds none.
        """
        try:
            prefs = self._prefs
            if prefs.get(PREF_KEY_CORRUPT):
                self._logger.error(
                    "Matter export allow-list: an earlier rescue copy already exists in the %r "
                    "plugin pref and was KEPT — this newer unreadable value was NOT preserved. "
                    "Recover from the existing copy, then clear it.",
                    PREF_KEY_CORRUPT,
                )
                return
            prefs[PREF_KEY_CORRUPT] = raw if isinstance(raw, str) else repr(raw)
        except Exception as exc:  # pylint: disable=broad-except
            # Preservation is best-effort: whatever the prefs mapping does, it
            # must not turn an unreadable allow-list into a failed startup.
            self._logger.exception(exc)
