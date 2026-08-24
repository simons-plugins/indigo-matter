"""Envelope, shapes and helpers for the plugin ⇄ bridge-node protocol.

The authoritative contract is `docs/BRIDGE_PROTOCOL.md`; a bare ``§N`` below
refers to it. This module is the Python half of that contract — the TypeScript
half is `bridge-node/src/protocol.ts`, and `tests/fixtures/bridge_protocol/`
holds the golden frames both suites assert against (§7).

Unlike :mod:`protocol` this is **not a rename firewall**: we author both peers
and ship them together, so a field rename here is a coordinated edit to both
ends in one release (§ preamble). What protects us instead is
``protocolVersion`` in the handshake, which :mod:`bridge_client` fails closed on.

What the rest of the plugin consumes is the normalised dataclasses below —
:class:`BridgeCommand`, :class:`StatusReport`, :class:`PairingReport`,
:class:`FabricInfo` — never raw frames. No Indigo dependency.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: Protocol version this plugin speaks (§2). Skew fails closed on both peers.
#:
#: Bumped 1 -> 2 alongside ``EndpointSpec.published_as`` first being sent
#: (issues #219/#240, PR5 design owner ruling 1): rather than gate ``published_as`` behind
#: a ``bridgeVersion`` capability check, the protocol simply requires v2. An
#: old (pre-#219/#240) node cannot silently ignore a ``publishedAs`` it does
#: not understand and publish a duplicate default-identity accessory, because
#: it never gets past the handshake with a v2-speaking plugin at all.
PROTOCOL_VERSION = 2

#: Loopback WS port the bridge node listens on (§ header); pref-configurable.
DEFAULT_WS_PORT = "5581"
PREF_WS_PORT = "bridgeWsPort"

# --------------------------------------------------------------------------
# Envelope (§1) — identical shapes to the controller protocol
# --------------------------------------------------------------------------
KEY_MESSAGE_ID = "message_id"
KEY_COMMAND = "command"
KEY_ARGS = "args"
KEY_RESULT = "result"
KEY_ERROR_CODE = "error_code"
KEY_ERROR_DETAILS = "details"
KEY_EVENT = "event"
KEY_DATA = "data"

# Handshake frame (§2) — a bare object, no envelope
KEY_PROTOCOL_VERSION = "protocolVersion"
KEY_BRIDGE_VERSION = "bridgeVersion"
KEY_MATTER_JS_VERSION = "matterJsVersion"

# --------------------------------------------------------------------------
# Commands (§3)
# --------------------------------------------------------------------------
CMD_ATTACH = "attach"
CMD_UPSERT_ENDPOINT = "upsert_endpoint"
CMD_REMOVE_ENDPOINT = "remove_endpoint"
CMD_SET_STATE = "set_state"
CMD_SET_REACHABLE = "set_reachable"
CMD_GET_STATUS = "get_status"
CMD_GET_PAIRING = "get_pairing"
CMD_OPEN_WINDOW = "open_commissioning_window"
CMD_REMOVE_FABRIC = "remove_fabric"
CMD_FACTORY_RESET = "factory_reset"
CMD_REBUILD_ENDPOINT_MAP = "rebuild_endpoint_map"
CMD_LIST_ORPHANS = "list_orphans"

#: The complete §3 command domain. Anything else gets ``unknown_command``.
COMMANDS = frozenset({
    CMD_ATTACH, CMD_UPSERT_ENDPOINT, CMD_REMOVE_ENDPOINT, CMD_SET_STATE, CMD_SET_REACHABLE,
    CMD_GET_STATUS, CMD_GET_PAIRING, CMD_OPEN_WINDOW, CMD_REMOVE_FABRIC, CMD_FACTORY_RESET,
    CMD_REBUILD_ENDPOINT_MAP, CMD_LIST_ORPHANS,
})

# attach args (§3.1)
ARG_PLUGIN_VERSION = "pluginVersion"
ARG_ENDPOINTS = "endpoints"
ARG_INTENT = "intent"
#: The §3.1 mass-removal opt-in. Without it the node refuses an attach that
#: would remove every live endpoint (``mass_removal_refused``).
INTENT_REPLACE_ALL = "replace_all"

# per-command args (§3.2-§3.10)
ARG_ENDPOINT = "endpoint"
ARG_INDIGO_DEVICE_ID = "indigoDeviceId"
#: Issues #219/#240 — the accessory identity this device publishes as (§4.1).
ARG_PUBLISHED_AS = "publishedAs"
ARG_STATES = "states"
ARG_REACHABLE = "reachable"
ARG_DURATION_SECONDS = "durationSeconds"
ARG_FABRIC_INDEX = "fabricIndex"
ARG_PRESERVE_ENDPOINT_NUMBERS = "preserveEndpointNumbers"

#: §3.8 default window duration; the node clamps to 180-900s.
DEFAULT_WINDOW_SECONDS = 900

# --------------------------------------------------------------------------
# Error codes (§1.1) — the complete domain for protocol version 2
# --------------------------------------------------------------------------
ERR_UNKNOWN_COMMAND = "unknown_command"
ERR_MALFORMED_ARGS = "malformed_args"
ERR_VERSION_MISMATCH = "version_mismatch"
ERR_NOT_ATTACHED = "not_attached"
ERR_UNKNOWN_DEVICE = "unknown_device"
ERR_UNKNOWN_ROLE = "unknown_role"
ERR_ROLE_CHANGE = "role_change"
ERR_MASS_REMOVAL_REFUSED = "mass_removal_refused"
ERR_ENDPOINT_MAP_INVALID = "endpoint_map_invalid"
ERR_COMMISSIONING_WINDOW_FAILED = "commissioning_window_failed"
ERR_INTERNAL = "internal"

ERROR_CODES = frozenset({
    ERR_UNKNOWN_COMMAND, ERR_MALFORMED_ARGS, ERR_VERSION_MISMATCH, ERR_NOT_ATTACHED,
    ERR_UNKNOWN_DEVICE, ERR_UNKNOWN_ROLE, ERR_ROLE_CHANGE, ERR_MASS_REMOVAL_REFUSED,
    ERR_ENDPOINT_MAP_INVALID, ERR_COMMISSIONING_WINDOW_FAILED, ERR_INTERNAL,
})

#: The ``details`` prefix a §1.1 refusal carries when the fault is an unusable
#: ``identity.json`` rather than an unusable endpoint map (``RefuseReason``
#: in ``bridge-node/src/protocol.ts``, mirrored here per BRIDGE_PROTOCOL §1.1).
#:
#: One error CODE covers every refuse-to-start reason — §1.1 defines the state,
#: not the cause — so the reason text is the only thing on the wire that says
#: which remedy applies, and the two are opposites. An unreadable MAP is fixed
#: by §3.11's rebuild. An unreadable IDENTITY is not, and cannot be: the node
#: refuses that rebuild outright, because clearing the refusal would leave the
#: bridge serving under a ``SerialNumber`` no paired ecosystem has ever seen —
#: the exact harm the refusal exists to prevent. Telling the user to rebuild
#: there sends them at the one door that is deliberately locked.
REFUSE_IDENTITY_UNREADABLE = "the bridge identity file is present but unreadable"

# --------------------------------------------------------------------------
# Events (§5)
# --------------------------------------------------------------------------
EVT_COMMAND = "command"
EVT_FABRICS_CHANGED = "fabrics_changed"
EVT_COMMISSIONED = "commissioned"
EVT_DECOMMISSIONED = "decommissioned"
EVT_WINDOW_CLOSED = "window_closed"
EVT_DRIFT_DETECTED = "drift_detected"

EVENT_NAMES = frozenset({
    EVT_COMMAND, EVT_FABRICS_CHANGED, EVT_COMMISSIONED, EVT_DECOMMISSIONED,
    EVT_WINDOW_CLOSED, EVT_DRIFT_DETECTED,
})

# --------------------------------------------------------------------------
# Roles (§4.2) — the two vocabularies each role defines
# --------------------------------------------------------------------------
# §4.2 is the only source for these, and it writes the light rows additively
# ("+ colorTempMireds"); they are spelled out in full here so nothing has to
# re-derive the inheritance. The export mapping (E2/E3) consumes them, and the
# golden-frame coverage test enumerates from here rather than a hand-kept list —
# add a role or a command and the fixtures are required to grow with it.

#: role → the ``set_state`` keys the plugin may push (§3.4/§4.2).
ROLE_STATE_KEYS = {
    "onOffPlugInUnit": ("onOff",),
    "onOffLight": ("onOff",),
    "dimmableLight": ("onOff", "level"),
    "colorTemperatureLight": ("onOff", "level", "colorTempMireds"),
    "extendedColorLight": ("onOff", "level", "colorTempMireds", "hue", "saturation"),
    "windowCovering": ("position",),
    "doorLock": ("locked",),
    "occupancySensor": ("occupied",),
    "contactSensor": ("contact",),
    # The leak family (issue #236). Three device types over one cluster:
    # BooleanState's `stateValue`, whose polarity for a *detector* is "true =
    # detected" — the opposite reading to `contactSensor`, where the same
    # attribute means "true = closed". Separate keys rather than one shared
    # `detected` so a role change cannot silently reinterpret a pushed state.
    "waterLeakDetector": ("leak",),
    "waterFreezeDetector": ("freeze",),
    "rainSensor": ("rain",),
    # Smoke and CO are two roles over ONE Matter device type (issue #179).
    # Smoke CO Alarm 0x0076 selects its sensing half by cluster feature, and an
    # Indigo sensor is one boolean that means one thing — publishing both
    # halves from it would tell an ecosystem a smoke-only sensor is also
    # watching for CO. The node derives the cluster's mandatory
    # `expressedState` from whichever of these it was given; the plugin never
    # sends it, because there is nothing in Indigo to send it from.
    "smokeAlarm": ("smoke",),
    "coAlarm": ("co",),
    "temperatureSensor": ("temperatureC",),
    "humiditySensor": ("humidityPct",),
    "lightSensor": ("lux",),
    "pressureSensor": ("pressureKPa",),
    "flowSensor": ("flowM3h",),
    "thermostat": ("localTemperatureC", "heatingSetpointC", "coolingSetpointC", "systemMode"),
}

#: ``set_state`` keys valid for ANY role whose export carries ``battery: true``
#: (§4.1/§4.2, issue #220) — the first role-INDEPENDENT state key. Deliberately
#: **not** folded into :data:`ROLE_STATE_KEYS`: that table is the per-role
#: contract the zoo test (``tests/test_export_handlers.py``) pins each
#: handler's ``states_for`` against, and ``batteryLevel`` is published by a
#: wrapper (``export_handlers.ExportHandler.published_states``) that sits
#: outside every role's own vocabulary on purpose — a device either has a
#: battery or it does not, independent of what it is exported *as*.
SHARED_STATE_KEYS = ("batteryLevel",)

#: role → the ``command`` event names the node emits for it (§5/§4.2). Sensors
#: are read-only in Matter, so their tuple is empty by design, not by omission.
ROLE_COMMANDS = {
    "onOffPlugInUnit": ("onOff",),
    "onOffLight": ("onOff",),
    "dimmableLight": ("onOff", "setLevel"),
    "colorTemperatureLight": ("onOff", "setLevel", "setColorTemp"),
    "extendedColorLight": ("onOff", "setLevel", "setColorTemp", "setColor"),
    "windowCovering": ("goToPosition", "stopMotion"),
    "doorLock": ("lock", "unlock"),
    "occupancySensor": (),
    "contactSensor": (),
    "waterLeakDetector": (),
    "waterFreezeDetector": (),
    "rainSensor": (),
    # `SelfTestRequest` is the cluster's only command and its conformance is
    # "O" — optional. Not declared, so these stay read-only like every other
    # exported sensor rather than advertising a self-test the plugin would
    # have nothing to run.
    "smokeAlarm": (),
    "coAlarm": (),
    "temperatureSensor": (),
    "humiditySensor": (),
    "lightSensor": (),
    "pressureSensor": (),
    "flowSensor": (),
    "thermostat": ("setHeatingSetpoint", "setCoolingSetpoint", "setSystemMode"),
}

#: The v1 role enum (§4.2) — derived, so a role can only exist with both of its
#: vocabularies declared. This set is what the allow-list validates against.
ROLES = frozenset(ROLE_STATE_KEYS)

#: The v1 ``systemMode`` domain, in both directions (§4.2).
SYSTEM_MODES = ("off", "heat", "cool", "auto")

# --------------------------------------------------------------------------
# Published identity (issues #219/#240) — the Python twin of
# `bridge-node/src/protocol.ts`'s `publishedIdFor`/`parsePublishedId`. Both
# derivations MUST agree on every input: this string is what the node keys
# `Endpoint.id` on, and a plugin/node disagreement about it would create a
# duplicate accessory rather than update the one the user meant.
# --------------------------------------------------------------------------
#: Matter's ``UniqueID`` cap (PR5 design F9, measured against matter.js 0.17.8) — mirrors
#: `protocol.ts`'s ``PUBLISHED_ID_MAX``.
PUBLISHED_ID_MAX = 32

_PUBLISHED_ID_PREFIX = "indigo-"
#: ``\Z``, not ``$``: Python's ``$`` also matches just before a trailing
#: newline, so ``"indigo-1\n"`` would parse here and be REFUSED by the
#: TypeScript twin — the one thing these two derivations may never do.
#:
#: ``re.ASCII`` for exactly the same reason, and it is the same class of bug:
#: Python's ``\d`` matches EVERY Unicode decimal digit, JavaScript's matches
#: ``[0-9]`` only. Without it ``"indigo-١٢٣"`` (Arabic-Indic digits) parses
#: here — ``int()`` is equally Unicode-aware, so it even yields 123 — while
#: ``parsePublishedId`` refuses it. A hand-edited ``.indiPref`` carrying one
#: would pass every plugin-side validation and then kill the whole attach
#: with ``malformed_args``, taking every export offline.
_PUBLISHED_ID_RE = re.compile(r"^indigo-(-?\d+)(?:~(\d+))?\Z", re.ASCII)
#: Python has no native "safe integer" concept (JS's ``Number.isSafeInteger``,
#: which `parsePublishedId` guards against); this is that same bound, so a
#: device id round-trips identically through either peer's derivation.
_JS_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True)
class PublishedId:
    """The parsed halves of a published identity (see :func:`parse_published_id`)."""
    device_id: int
    generation: int


def published_id_for(indigo_device_id: int, generation: int = 1) -> str:
    """``publishedIdFor``'s Python twin — generation 1 is today's derivation."""
    if generation == 1:
        return f"{_PUBLISHED_ID_PREFIX}{indigo_device_id}"
    return f"{_PUBLISHED_ID_PREFIX}{indigo_device_id}~{generation}"


def parse_published_id(value: str) -> Optional[PublishedId]:
    """The inverse of :func:`published_id_for`, or ``None`` for anything that
    is not a lawful published identity — strict rather than forgiving, the
    same way ``parsePublishedId`` is on the TypeScript side: a loosely coerced
    device id is a new accessory in every paired ecosystem, not the one the
    caller meant. The length cap is PR5 design F9's measured ``UniqueID`` limit.
    """
    if not isinstance(value, str) or len(value) > PUBLISHED_ID_MAX:
        return None
    match = _PUBLISHED_ID_RE.match(value)
    if match is None:
        return None
    device_id = int(match.group(1))
    if abs(device_id) > _JS_MAX_SAFE_INTEGER:
        return None
    if match.group(2) is None:
        return PublishedId(device_id=device_id, generation=1)
    generation = int(match.group(2))
    if generation < 2:
        return None
    return PublishedId(device_id=device_id, generation=generation)


def next_generation(published_as: str) -> str:
    """The published identity one role-change generation past ``published_as``
    (issue #240) — ``indigo-<id>`` -> ``indigo-<id>~2`` -> ``indigo-<id>~3`` ...

    The plugin is the only peer that ever bumps a generation (PR5 design §1.3): the node
    only ever receives whatever this produces. Raises ``ValueError`` for an
    unparseable ``published_as`` — every caller already holds either a stored,
    previously-validated identity or the default derivation, so an unlawful
    value here is a bug, not a wire input to tolerate.
    """
    parsed = parse_published_id(published_as)
    if parsed is None:
        raise ValueError(f"not a lawful published identity: {published_as!r}")
    return published_id_for(parsed.device_id, parsed.generation + 1)


class BridgeProtocolError(Exception):
    """Raised when a response frame carries an ``error_code`` (§1.1)."""

    def __init__(self, code: Any, details: str = "") -> None:
        super().__init__(f"bridge node error {code}: {details}")
        self.code = code
        self.details = details


# --------------------------------------------------------------------------
# Shapes (§4) — normalised for the plugin, snake_case on this side of the wire
# --------------------------------------------------------------------------
@dataclass
class Hello:
    """The bare handshake frame the node sends on every connection (§2)."""
    protocol_version: int
    bridge_version: str
    matter_js_version: str


@dataclass
class EndpointSpec:
    """One exported accessory (§4.1).

    ``indigo_device_id`` is the DRIVING device — what §5 commands are
    addressed to and what §3.4 state pushes are keyed on. :attr:`published_as`
    is the accessory's identity (ADR-0010, issues #219/#240), defaulting to
    ``indigo-<indigo_device_id>``.
    """
    indigo_device_id: int
    role: str
    label: str
    reachable: bool = True
    states: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    #: §4.1 issue #220 — "ensure this accessory publishes PowerSource". False
    #: (the default) means "no evidence right now", never a removal request —
    #: the node's live cluster set is monotonic (docs/BRIDGE_PROTOCOL.md §4.1).
    battery: bool = False
    #: Issues #219/#240 — the accessory identity this device publishes as
    #: (see :func:`published_id_for`). ``""`` means "use today's default
    #: derivation" — :meth:`to_wire` omits the key in that case (and also when
    #: the value IS the default), so an ordinary export's wire frame is
    #: byte-identical to what this plugin has always sent.
    published_as: str = ""

    def to_wire(self) -> dict:
        """The §4.1 wire shape. ``battery`` is omitted unless ``True`` — never
        spelled ``false`` on the wire (round-trip tests are exact).
        ``publishedAs`` is omitted whenever it is empty or equal to the
        default derivation, for the same reason."""
        wire = {
            ARG_INDIGO_DEVICE_ID: int(self.indigo_device_id),
            "role": self.role,
            "label": self.label,
            ARG_REACHABLE: bool(self.reachable),
            ARG_STATES: dict(self.states),
            "options": dict(self.options),
        }
        if self.battery:
            wire["battery"] = True
        if self.published_as and self.published_as != published_id_for(self.indigo_device_id):
            wire[ARG_PUBLISHED_AS] = self.published_as
        return wire

    @classmethod
    def from_wire(cls, data: dict) -> "EndpointSpec":
        """Rebuild a spec from its §4.1 wire shape."""
        return cls(
            indigo_device_id=int(data[ARG_INDIGO_DEVICE_ID]),
            role=data["role"],
            label=data.get("label", ""),
            reachable=bool(data.get(ARG_REACHABLE, True)),
            states=dict(data.get(ARG_STATES) or {}),
            options=dict(data.get("options") or {}),
            battery=bool(data.get("battery", False)),
            published_as=str(data.get(ARG_PUBLISHED_AS, "") or ""),
        )


@dataclass
class EndpointSummary:
    """A live endpoint as the node reports it in a :class:`StatusReport` (§4.3)."""
    indigo_device_id: int
    endpoint_number: int
    role: str
    #: Issues #219/#240 — informational; see :attr:`EndpointSpec.published_as`.
    #: Tolerant default so a report from a pre-PR5 node still parses.
    published_as: str = ""


@dataclass
class DriftEntry:
    """A ``UniqueID → endpointNumber`` mapping that moved (§4.3, PRD §4.3).

    Surfaced as a plugin error, never auto-repaired: silent reallocation
    duplicates accessories in every paired ecosystem.
    """
    unique_id: str
    expected: int
    actual: int


@dataclass
class OrphanRecord:
    """One left-behind accessory identity (§3.12, issues #219/#240) — the
    re-adopt picker's data. Mirrors `bridge-node/src/protocol.ts`'s
    ``OrphanRecord`` field-for-field. ``role``/``label`` absent together is
    the pre-2026.16.2 bare orphan (PR5 design §4.2's third picker row, E4) —
    a plugin that old deleted them on un-export, so there is nothing left to
    match a replacement device against and the record can never be
    re-adopted, only shown so the user can see its number is spoken for.
    """
    unique_id: str
    number: int
    role: Optional[str] = None
    label: Optional[str] = None
    #: ISO-8601, or ``None`` for a pre-PR5 orphan — the picker renders that
    #: as "date unknown" (§4.2).
    orphaned_at: Optional[str] = None
    #: The Indigo device that drove this identity before it was un-exported,
    #: if the node recorded one (issue #219).
    device_id: Optional[int] = None


@dataclass
class FabricInfo:
    """One commissioned ecosystem (§4.3)."""
    fabric_index: int
    label: str
    vendor_id: int


@dataclass
class ChurnPeer:
    """One controller peer the node's churn detector has over threshold
    (§4.3, issues #283/#286). Peers are individual because a fabric can hold
    several and they are not interchangeable — two Echoes on one Alexa fabric
    churn independently.
    """
    peer_node_id: str
    fabric_index: int
    live_sessions: int
    invalid_deletions: int
    window_minutes: int
    #: ISO-8601 — when this peer FIRST crossed a threshold, not when it last did.
    since: str


@dataclass
class SubscriptionChurn:
    """Controller subscription churn against this bridge (§4.3, issues #283/#286).

    ``checked: False`` is **not** the healthy answer — it means the node's
    detector could not observe session state at all, the same rule
    :attr:`StatusReport.drift_checked` carries for ``drift``. Defaulted so a
    report from a pre-0.15.0 node (which never sends this field at all) still
    parses, to the honest reading: such a node never looked.
    """
    checked: bool = False
    active: bool = False
    peers: list = field(default_factory=list)


@dataclass
class SessionHygienePeer:
    """One peer's live CASE session count (§4.3, issue #283 "Finding 2").

    Unlike :class:`ChurnPeer` this is EVERY peer holding a live CASE session,
    not only ones over a churn threshold — it is issue #283's own "diagnostic
    to run first when staleness recurs" (count live CASE sessions per peer),
    so a human can see a pile *forming* before ``subscription_churn`` (which
    only reports over-threshold peers) would.
    """
    peer_node_id: str
    fabric_index: int
    live_sessions: int


@dataclass
class SessionHygieneClosed:
    """Sessions this node has force-closed since it started, by reason
    (§4.3, issue #283 "Finding 2"). Cumulative — never decreases within a run.
    """
    superseded: int = 0
    dead: int = 0
    rotated: int = 0


@dataclass
class SessionHygiene:
    """App-level CASE session hygiene against this bridge (§4.3, issue #283
    "Finding 2").

    ``checked: False`` is **not** the healthy answer, the same rule
    :class:`SubscriptionChurn` carries: it means the node's hygiene machinery
    could not observe/act on the session layer at all, not that nothing
    needed closing. Defaulted so a report from a pre-0.17.0 node (which never
    sends this field at all) still parses, to the honest reading: such a node
    never looked and never acted.

    ``sent`` is what lets a caller tell that absence apart from a CURRENT node
    reporting ``checked=False`` — both collapse to the same ``checked=False``
    default above (deliberately: an absent field means "never looked" too),
    but they warrant different treatment: a pre-0.17.0 node has nothing wrong
    with it, while a 0.17.0+ node with ``checked=False`` has a mitigation that
    just stopped running. See :meth:`ExportBridge._apply_session_hygiene`.
    """
    checked: bool = False
    peers: list = field(default_factory=list)
    closed: SessionHygieneClosed = field(default_factory=SessionHygieneClosed)
    sent: bool = False


@dataclass
class StatusReport:
    """The result of ``attach``/``get_status``/``rebuild_endpoint_map`` (§4.3)."""
    commissioned: bool
    fabrics: list
    endpoint_count: int
    endpoints: list
    drift: list
    #: Whether ``drift`` is an answer or an absence. ``drift: []`` alone is
    #: ambiguous — "checked, nothing moved" and "there is no baseline to check
    #: against" are opposites — so an empty ``drift`` is only an all-clear when
    #: this is true. Defaulted so a report from a pre-warnings node still parses.
    drift_checked: bool = False
    #: §4.3 — persistence failures the node hit and cannot fix on its own. The
    #: node's only other channel is stdout, and in this milestone it is started
    #: by hand, so stdout is a terminal nobody is watching. Current, not
    #: historical: an entry disappears when the operation it describes succeeds.
    warnings: list = field(default_factory=list)
    #: §4.3, issue #286 — controller subscription churn, additive since
    #: bridge-node 0.15.0 with no ``protocolVersion`` bump (the same precedent
    #: ``drift_checked``/``warnings`` set). Defaulted so a report from an older
    #: node — which never sends this field — parses as ``checked=False``: it
    #: never looked, which is "unknown", never "healthy".
    subscription_churn: SubscriptionChurn = field(default_factory=SubscriptionChurn)
    #: §4.3, issue #283 "Finding 2" — app-level CASE session hygiene,
    #: additive since bridge-node 0.17.0 with no ``protocolVersion`` bump
    #: (the same precedent ``subscription_churn`` set). Defaulted so a report
    #: from an older node — which never sends this field — parses as
    #: ``checked=False``: it never looked, which is "unknown", never "healthy".
    session_hygiene: SessionHygiene = field(default_factory=SessionHygiene)


@dataclass
class PairingReport:
    """The result of ``get_pairing`` (§3.7).

    ``manual_pairing_code``/``qr_pairing_code`` are non-null only while a window
    is open — a passcode is not durable once the first fabric commissions.
    """
    commissioned: bool
    window_open: bool
    window_expires_at: Optional[str]
    manual_pairing_code: Optional[str]
    qr_pairing_code: Optional[str]
    fabrics: list


@dataclass
class CommissioningWindow:
    """The result of ``open_commissioning_window`` (§3.8)."""
    manual_pairing_code: str
    qr_pairing_code: str
    window_expires_at: str


@dataclass
class FabricRemoval:
    """The result of ``remove_fabric`` (§3.9) — what it DID, not that it returned.

    The node used to answer ``{}`` whether it dropped a fabric or found nothing
    at the index, so the unpair menu reported "that ecosystem has been unpaired.
    Every accessory has been removed" over a node-side no-op. The stale index is
    not an edge case: the picker is built from the CACHED fabric list, so an
    ecosystem that unpaired *us* since the last §5 event is the designed way to
    land there.

    ``remaining`` is ``None`` — never a number — when the node could not read
    its own fabric count (legitimate while matter.js rebuilds after a last-fabric
    leave). Defaulted so a report from a node predating this result still parses,
    and defaulted to the truthful direction: an old node that answered ``{}``
    really had removed something.
    """
    removed: bool = True
    remaining: Optional[int] = None


@dataclass
class BridgeCommand:
    """An ecosystem-originated action, from a ``command`` event (§5).

    ``command`` and ``args`` are exactly as enumerated per role in §4.2. The
    plugin resolves ``indigo_device_id`` through the allow-list before acting.
    """
    indigo_device_id: int
    command: str
    args: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_hello(frame: Any) -> Hello:
    """Validate the bare handshake frame (§2).

    Raises :class:`BridgeProtocolError` when it is not a hello at all — a node
    that opens with anything else is not speaking this protocol, and attaching
    to it would be worse than refusing.
    """
    if not isinstance(frame, dict) or KEY_PROTOCOL_VERSION not in frame:
        raise BridgeProtocolError(ERR_MALFORMED_ARGS, f"not a handshake frame: {frame!r}")
    version = frame[KEY_PROTOCOL_VERSION]
    if not isinstance(version, int) or isinstance(version, bool):
        raise BridgeProtocolError(ERR_MALFORMED_ARGS, f"non-integer protocolVersion: {version!r}")
    return Hello(
        protocol_version=version,
        bridge_version=str(frame.get(KEY_BRIDGE_VERSION, "unknown")),
        matter_js_version=str(frame.get(KEY_MATTER_JS_VERSION, "unknown")),
    )


def _parse_fabric(item: Any) -> Optional[FabricInfo]:
    """One §4.3 fabric, tolerantly — ``None`` for anything malformed.

    A single bad entry (not a dict, a missing ``fabricIndex``, a JSON
    ``null``, a field that will not ``int()``) must degrade the ``fabrics``
    LIST, not detonate the whole ``StatusReport`` (issue #288 review finding
    D, the same reasoning :func:`_parse_churn_peer` documents for
    ``peers``): an exception escaping here used to fail the entire status
    poll — and, on the attach path, the attach itself — over one fabric
    entry, which since issue #288 is load-bearing for the per-fabric slot
    plan, not merely descriptive.
    """
    if not isinstance(item, dict):
        return None
    try:
        return FabricInfo(
            fabric_index=int(item[ARG_FABRIC_INDEX]),
            label=str(item.get("label", "") or ""),
            vendor_id=int(item.get("vendorId", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_fabrics(data: Any) -> list:
    """Normalise a list of §4.3 ``FabricInfo`` objects. Tolerant per-entry —
    see :func:`_parse_fabric`."""
    return [fabric for fabric in (_parse_fabric(item) for item in (data or []))
            if fabric is not None]


def parse_status(result: Any) -> StatusReport:
    """Normalise a ``StatusReport`` payload (§4.3)."""
    data = result or {}
    return StatusReport(
        commissioned=bool(data.get("commissioned", False)),
        fabrics=parse_fabrics(data.get("fabrics")),
        endpoint_count=int(data.get("endpointCount", 0)),
        endpoints=[
            EndpointSummary(
                indigo_device_id=int(item[ARG_INDIGO_DEVICE_ID]),
                endpoint_number=int(item["endpointNumber"]),
                role=str(item.get("role", "")),
                published_as=str(item.get(ARG_PUBLISHED_AS, "") or ""),
            )
            for item in (data.get("endpoints") or [])
        ],
        drift=parse_drift(data.get("drift")),
        drift_checked=bool(data.get("driftChecked", False)),
        warnings=[str(item) for item in (data.get("warnings") or [])],
        subscription_churn=parse_subscription_churn(data.get("subscriptionChurn")),
        session_hygiene=parse_session_hygiene(data.get("sessionHygiene")),
    )


def parse_drift(data: Any) -> list:
    """Normalise a list of §4.3 drift entries."""
    return [
        DriftEntry(
            unique_id=str(item.get("uniqueId", "")),
            expected=int(item.get("expected", 0)),
            actual=int(item.get("actual", 0)),
        )
        for item in (data or [])
    ]


def _parse_churn_peer(item: Any) -> Optional[ChurnPeer]:
    """One §4.3 churn peer, tolerantly — ``None`` for anything malformed.

    A single bad entry (not a dict, or a field that will not ``int()``) must
    degrade the ``peers`` LIST, not detonate the whole ``StatusReport`` (issue
    #286 review finding 4): an exception escaping here previously failed the
    entire status poll at DEBUG, which also suppressed
    ``_report_node_warnings`` for the whole report — including the churn
    warning itself — and could fail an ``attach`` outright.
    """
    if not isinstance(item, dict):
        return None
    try:
        return ChurnPeer(
            peer_node_id=str(item.get("peerNodeId", "")),
            fabric_index=int(item.get("fabricIndex", 0)),
            live_sessions=int(item.get("liveSessions", 0)),
            invalid_deletions=int(item.get("invalidDeletions", 0)),
            window_minutes=int(item.get("windowMinutes", 0)),
            since=str(item.get("since", "")),
        )
    except (TypeError, ValueError):
        return None


def parse_subscription_churn(data: Any) -> SubscriptionChurn:
    """Normalise the §4.3 ``subscriptionChurn`` object (issues #283/#286).

    Tolerant of absence — a pre-0.15.0 node's ``StatusReport`` has no such key
    at all — and of anything not shaped like the object, both of which fall
    back to the dataclass's own ``checked=False`` default: a node that never
    sent this field never looked, which is "unknown", never "healthy".
    """
    if not isinstance(data, dict):
        return SubscriptionChurn()
    return SubscriptionChurn(
        checked=bool(data.get("checked", False)),
        active=bool(data.get("active", False)),
        peers=[peer for peer in (_parse_churn_peer(item) for item in (data.get("peers") or []))
               if peer is not None],
    )


def _parse_hygiene_peer(item: Any) -> Optional[SessionHygienePeer]:
    """One §4.3 session-hygiene peer, tolerantly — ``None`` for anything
    malformed. Same per-entry degradation as :func:`_parse_churn_peer`
    (issue #286 review finding 4): one bad entry must thin the ``peers``
    LIST, not fail the whole ``StatusReport``."""
    if not isinstance(item, dict):
        return None
    try:
        return SessionHygienePeer(
            peer_node_id=str(item.get("peerNodeId", "")),
            fabric_index=int(item.get("fabricIndex", 0)),
            live_sessions=int(item.get("liveSessions", 0)),
        )
    except (TypeError, ValueError):
        return None


def parse_session_hygiene(data: Any) -> SessionHygiene:
    """Normalise the §4.3 ``sessionHygiene`` object (issue #283 "Finding 2").

    Tolerant of absence — a pre-0.17.0 node's ``StatusReport`` has no such
    key at all — and of anything not shaped like the object, both of which
    fall back to the dataclass's own ``checked=False`` default: a node that
    never sent this field never looked, which is "unknown", never "healthy".
    A malformed ``closed`` block degrades to all-zero rather than failing the
    whole report — the counts are informational, and a status poll must
    never fail over them.

    ``sent`` records only whether ``data`` was present and dict-shaped, NOT
    whether it parsed cleanly — that is what ``ExportBridge._apply_session_hygiene``
    needs to tell "an old node never sent this" apart from "a current node
    sent it and reports ``checked=False``".
    """
    if not isinstance(data, dict):
        return SessionHygiene()
    closed_data = data.get("closed")
    closed = SessionHygieneClosed()
    if isinstance(closed_data, dict):
        try:
            closed = SessionHygieneClosed(
                superseded=int(closed_data.get("superseded", 0)),
                dead=int(closed_data.get("dead", 0)),
                rotated=int(closed_data.get("rotated", 0)),
            )
        except (TypeError, ValueError):
            closed = SessionHygieneClosed()
    return SessionHygiene(
        checked=bool(data.get("checked", False)),
        peers=[peer for peer in (_parse_hygiene_peer(item) for item in (data.get("peers") or []))
               if peer is not None],
        closed=closed,
        sent=True,
    )


def parse_orphans(result: Any) -> list:
    """Normalise a §3.12 ``list_orphans`` payload into :class:`OrphanRecord`."""
    orphans = []
    for item in (result or []):
        raw_device_id = item.get("deviceId")
        orphans.append(OrphanRecord(
            unique_id=str(item.get("uniqueId", "")),
            number=int(item.get("number", 0)),
            role=item.get("role"),
            label=item.get("label"),
            orphaned_at=item.get("orphanedAt"),
            device_id=int(raw_device_id) if raw_device_id is not None else None,
        ))
    return orphans


def parse_pairing(result: Any) -> PairingReport:
    """Normalise a ``get_pairing`` payload (§3.7)."""
    data = result or {}
    return PairingReport(
        commissioned=bool(data.get("commissioned", False)),
        window_open=bool(data.get("windowOpen", False)),
        window_expires_at=data.get("windowExpiresAt"),
        manual_pairing_code=data.get("manualPairingCode"),
        qr_pairing_code=data.get("qrPairingCode"),
        fabrics=parse_fabrics(data.get("fabrics")),
    )


def parse_window(result: Any) -> CommissioningWindow:
    """Normalise an ``open_commissioning_window`` payload (§3.8).

    Every field is required. A missing pairing code defaulted to ``""`` would be
    handed to the user as the code to type into their ecosystem — a window that
    silently opened with no way to use it is worse than a failed one.
    """
    data = result if isinstance(result, dict) else {}
    missing = [key for key in ("manualPairingCode", "qrPairingCode", "windowExpiresAt")
               if not data.get(key)]
    if missing:
        raise BridgeProtocolError(
            ERR_MALFORMED_ARGS,
            f"open_commissioning_window result is missing {', '.join(missing)}: {result!r}")
    return CommissioningWindow(
        manual_pairing_code=str(data["manualPairingCode"]),
        qr_pairing_code=str(data["qrPairingCode"]),
        window_expires_at=str(data["windowExpiresAt"]),
    )


def parse_fabric_removal(result: Any) -> FabricRemoval:
    """Normalise a ``remove_fabric`` payload (§3.9).

    ``removed`` defaults to True and ``remaining`` to None so a node predating
    this result (which answered ``{}`` and only ever answered it after a real
    ``leave()``) is read as the removal it was, not as a no-op. A present-but-
    unreadable ``remaining`` is None for the same reason the node sends null
    there: a fabricated count is exactly the lie this shape exists to end.
    """
    data = result if isinstance(result, dict) else {}
    raw_remaining = data.get("remaining")
    try:
        remaining = None if raw_remaining is None else int(raw_remaining)
    except (TypeError, ValueError):
        remaining = None
    return FabricRemoval(removed=bool(data.get("removed", True)), remaining=remaining)


def parse_command(data: Any) -> BridgeCommand:
    """Normalise a ``command`` event's ``data`` (§5)."""
    payload = data or {}
    return BridgeCommand(
        indigo_device_id=int(payload[ARG_INDIGO_DEVICE_ID]),
        command=str(payload.get(KEY_COMMAND, "")),
        args=dict(payload.get(KEY_ARGS) or {}),
    )


class BridgeProtocol:
    """Builds outbound frames and classifies inbound ones (§1).

    Deliberately the same shape as :class:`protocol.Protocol` so the shared
    :mod:`ws_json_client` machinery drives either peer unchanged.
    """

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def next_id(self) -> str:
        """The next opaque ``message_id`` (§1); echoed verbatim by the node."""
        return str(next(self._ids))

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    def build_request(self, command: str, args: Optional[dict] = None,
                      message_id: Optional[str] = None) -> dict:
        """The §1 request envelope."""
        return {
            KEY_MESSAGE_ID: message_id or self.next_id(),
            KEY_COMMAND: command,
            KEY_ARGS: dict(args) if args else {},
        }

    def build_attach(self, plugin_version: str, endpoints: Any,
                     *, replace_all: bool = False, message_id: Optional[str] = None) -> dict:
        """Build the §3.1 ``attach`` — declaration plus full desired endpoint set.

        ``replace_all`` adds ``intent: "replace_all"``, the §3.1 opt-in for an
        attach that would remove every live endpoint. It is passed only when the
        caller genuinely means it (the allow-list was emptied); an empty set
        WITHOUT it is left to be refused by the node with
        ``mass_removal_refused``, which is the whole point of the guard — a
        stale or half-initialised client must not silently un-export everything.
        """
        args: dict = {
            KEY_PROTOCOL_VERSION: PROTOCOL_VERSION,
            ARG_PLUGIN_VERSION: plugin_version,
            ARG_ENDPOINTS: [_endpoint_wire(spec) for spec in (endpoints or [])],
        }
        if replace_all:
            args[ARG_INTENT] = INTENT_REPLACE_ALL
        return self.build_request(CMD_ATTACH, args, message_id)

    def build_upsert_endpoint(self, spec: Any, message_id: Optional[str] = None) -> dict:
        """§3.2 — create or update one endpoint. Idempotent."""
        return self.build_request(CMD_UPSERT_ENDPOINT, {ARG_ENDPOINT: _endpoint_wire(spec)}, message_id)

    def build_remove_endpoint(self, indigo_device_id: int, message_id: Optional[str] = None) -> dict:
        """§3.3 — remove one endpoint; the number allocation is retained."""
        return self.build_request(CMD_REMOVE_ENDPOINT, {ARG_INDIGO_DEVICE_ID: int(indigo_device_id)}, message_id)

    def build_set_state(self, indigo_device_id: int, states: dict,
                        message_id: Optional[str] = None) -> dict:
        """§3.4 — push Indigo state outward; ``states`` keys are role-specific (§4.2)."""
        return self.build_request(
            CMD_SET_STATE,
            {ARG_INDIGO_DEVICE_ID: int(indigo_device_id), ARG_STATES: dict(states)},
            message_id,
        )

    def build_set_reachable(self, indigo_device_id: int, reachable: bool,
                            message_id: Optional[str] = None) -> dict:
        """§3.5 — Bridged Device Basic Information ``Reachable``, not a cluster state."""
        return self.build_request(
            CMD_SET_REACHABLE,
            {ARG_INDIGO_DEVICE_ID: int(indigo_device_id), ARG_REACHABLE: bool(reachable)},
            message_id,
        )

    def build_get_status(self, message_id: Optional[str] = None) -> dict:
        """§3.6 — the live endpoint/fabric/drift report."""
        return self.build_request(CMD_GET_STATUS, None, message_id)

    def build_get_pairing(self, message_id: Optional[str] = None) -> dict:
        """§3.7 — pairing state and the current codes, if any."""
        return self.build_request(CMD_GET_PAIRING, None, message_id)

    def build_open_window(self, duration_seconds: int = DEFAULT_WINDOW_SECONDS,
                          message_id: Optional[str] = None) -> dict:
        """§3.8 — open an enhanced commissioning window for another ecosystem."""
        return self.build_request(CMD_OPEN_WINDOW, {ARG_DURATION_SECONDS: int(duration_seconds)}, message_id)

    def build_remove_fabric(self, fabric_index: int, message_id: Optional[str] = None) -> dict:
        """§3.9 — drop one ecosystem's fabric."""
        return self.build_request(CMD_REMOVE_FABRIC, {ARG_FABRIC_INDEX: int(fabric_index)}, message_id)

    def build_factory_reset(self, preserve_endpoint_numbers: bool = True,
                            message_id: Optional[str] = None) -> dict:
        """§3.10 — wipe commissioning credentials and re-advertise."""
        # §3.10: the map is preserved by DEFAULT — a reset must not scramble
        # identities if the user re-pairs the same ecosystems.
        return self.build_request(
            CMD_FACTORY_RESET,
            {ARG_PRESERVE_ENDPOINT_NUMBERS: bool(preserve_endpoint_numbers)},
            message_id,
        )

    def build_rebuild_endpoint_map(self, message_id: Optional[str] = None) -> dict:
        """§3.11 — adopt the live endpoint numbers as the new persisted map."""
        return self.build_request(CMD_REBUILD_ENDPOINT_MAP, None, message_id)

    def build_list_orphans(self, message_id: Optional[str] = None) -> dict:
        """§3.12 — every left-behind accessory identity the re-adopt picker could offer."""
        return self.build_request(CMD_LIST_ORPHANS, None, message_id)

    # ------------------------------------------------------------------
    # Inbound classification
    # ------------------------------------------------------------------
    @staticmethod
    def is_event(frame: dict) -> bool:
        """True for an unsolicited event frame (§1)."""
        return KEY_EVENT in frame

    @staticmethod
    def is_response(frame: dict) -> bool:
        """True for a frame answering a request (§1)."""
        return KEY_EVENT not in frame and KEY_MESSAGE_ID in frame

    @staticmethod
    def message_id_of(frame: dict) -> Optional[str]:
        """The correlation id (§1)."""
        return frame.get(KEY_MESSAGE_ID)

    @staticmethod
    def error_of(frame: dict) -> Optional[tuple]:
        """``(code, details)`` for an error response (§1), else ``None``."""
        if KEY_ERROR_CODE not in frame:
            return None
        return frame.get(KEY_ERROR_CODE), str(frame.get(KEY_ERROR_DETAILS, ""))

    # ------------------------------------------------------------------
    # Inbound parsing
    # ------------------------------------------------------------------
    def parse_result(self, frame: dict) -> Any:
        """Return the result payload, or raise :class:`BridgeProtocolError`."""
        error = self.error_of(frame)
        if error is not None:
            raise BridgeProtocolError(*error)
        return frame.get(KEY_RESULT)

    @staticmethod
    def event_name(frame: dict) -> str:
        """The §5 event name."""
        return str(frame.get(KEY_EVENT, ""))

    @staticmethod
    def event_data(frame: dict) -> dict:
        """The §5 event payload (never ``None``)."""
        return frame.get(KEY_DATA) or {}


def _endpoint_wire(spec: Any) -> dict:
    """Accept either an :class:`EndpointSpec` or an already-wire-shaped dict."""
    return spec.to_wire() if isinstance(spec, EndpointSpec) else dict(spec)
