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
from dataclasses import dataclass, field
from typing import Any, Optional

#: Protocol version this plugin speaks (§2). Skew fails closed on both peers.
PROTOCOL_VERSION = 1

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

#: The complete §3 command domain. Anything else gets ``unknown_command``.
COMMANDS = frozenset({
    CMD_ATTACH, CMD_UPSERT_ENDPOINT, CMD_REMOVE_ENDPOINT, CMD_SET_STATE, CMD_SET_REACHABLE,
    CMD_GET_STATUS, CMD_GET_PAIRING, CMD_OPEN_WINDOW, CMD_REMOVE_FABRIC, CMD_FACTORY_RESET,
    CMD_REBUILD_ENDPOINT_MAP,
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
ARG_STATES = "states"
ARG_REACHABLE = "reachable"
ARG_DURATION_SECONDS = "durationSeconds"
ARG_FABRIC_INDEX = "fabricIndex"
ARG_PRESERVE_ENDPOINT_NUMBERS = "preserveEndpointNumbers"

#: §3.8 default window duration; the node clamps to 180-900s.
DEFAULT_WINDOW_SECONDS = 900

# --------------------------------------------------------------------------
# Error codes (§1.1) — the complete domain for protocol version 1
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
    "temperatureSensor": ("temperatureC",),
    "humiditySensor": ("humidityPct",),
    "lightSensor": ("lux",),
    "pressureSensor": ("pressureKPa",),
    "flowSensor": ("flowM3h",),
    "thermostat": ("localTemperatureC", "heatingSetpointC", "coolingSetpointC", "systemMode"),
}

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
    """One exported accessory (§4.1). ``indigo_device_id`` is the identity key."""
    indigo_device_id: int
    role: str
    label: str
    reachable: bool = True
    states: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        """The §4.1 wire shape."""
        return {
            ARG_INDIGO_DEVICE_ID: int(self.indigo_device_id),
            "role": self.role,
            "label": self.label,
            ARG_REACHABLE: bool(self.reachable),
            ARG_STATES: dict(self.states),
            "options": dict(self.options),
        }

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
        )


@dataclass
class EndpointSummary:
    """A live endpoint as the node reports it in a :class:`StatusReport` (§4.3)."""
    indigo_device_id: int
    endpoint_number: int
    role: str


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
class FabricInfo:
    """One commissioned ecosystem (§4.3)."""
    fabric_index: int
    label: str
    vendor_id: int


@dataclass
class StatusReport:
    """The result of ``attach``/``get_status``/``rebuild_endpoint_map`` (§4.3)."""
    commissioned: bool
    fabrics: list
    endpoint_count: int
    endpoints: list
    drift: list


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


def parse_fabrics(data: Any) -> list:
    """Normalise a list of §4.3 ``FabricInfo`` objects."""
    return [
        FabricInfo(
            fabric_index=int(item[ARG_FABRIC_INDEX]),
            label=str(item.get("label", "")),
            vendor_id=int(item.get("vendorId", 0)),
        )
        for item in (data or [])
    ]


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
            )
            for item in (data.get("endpoints") or [])
        ],
        drift=parse_drift(data.get("drift")),
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
        """§3.11 — reallocate endpoint numbers from scratch (duplicates accessories)."""
        return self.build_request(CMD_REBUILD_ENDPOINT_MAP, None, message_id)

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
