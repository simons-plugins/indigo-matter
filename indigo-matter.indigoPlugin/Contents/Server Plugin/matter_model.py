"""Normalised view of a matter-server node (Indigo-free, testable).

matter-server's ``get_node`` returns endpoints as ``{"1": {"clusters": [6,29,3],
"device_types": [...]}}`` and attributes as a flat ``{"<ep>/<cluster>/<attr>":
value}`` map. The cluster handlers want a tidy node/endpoint object, so this
module adapts the raw dict once and the rest of the code works in those terms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from protocol import Protocol

def node_id_to_str(node_id: Any) -> str:
    """Represent a Matter node id as the hex string the API uses.

    The canonical home for this helper (model-level, dependency-free):
    ``commission_jobs`` re-exports it, ``device_sync`` stamps it into each
    Indigo device's ``address`` so the node id is recoverable from the UI.
    """
    if isinstance(node_id, str):
        return node_id
    return f"0x{int(node_id):X}"


# BasicInformation cluster (0x0028) attribute ids used for device identity.
_BASIC_INFO = 0x0028
_ATTR_VENDOR_NAME = 0x0001
_ATTR_VENDOR_ID = 0x0002
_ATTR_PRODUCT_NAME = 0x0003
_ATTR_PRODUCT_ID = 0x0004
_ATTR_SW_VERSION_STRING = 0x000A

# BridgedDeviceBasicInformation cluster (0x0039) — per-endpoint identity for
# bridged child devices (Hue bulbs, Aqara sensors, etc. behind a bridge hub).
# device_sync imports these to route the per-endpoint Reachable attribute update.
CLUSTER_BRIDGED_BASIC = 0x0039
_BBRIDGE_ATTR_VENDOR_NAME = 0x0001    # VendorName  (optional)
_BBRIDGE_ATTR_PRODUCT_NAME = 0x0003  # ProductName (optional)
_BBRIDGE_ATTR_NODE_LABEL = 0x0005    # NodeLabel   (user-settable name)
BBRIDGE_ATTR_REACHABLE = 0x0011      # Reachable   (bool, per-endpoint liveness)


@dataclass(frozen=True)
class EndpointInfo:
    endpoint_id: int
    cluster_ids: frozenset[int]
    device_types: tuple[int, ...] = ()
    # Per-endpoint identity from BridgedDeviceBasicInformation (0x0039).
    # Populated only when the endpoint carries cluster 0x0039 (bridged child);
    # empty strings otherwise.  device_sync uses these for naming.
    node_label: str = ""
    vendor_name: str = ""
    product_name: str = ""

    def has(self, cluster_id: int) -> bool:
        return cluster_id in self.cluster_ids


@dataclass
class NodeInfo:
    node_id: int
    suggested_name: str = ""
    vendor_id: Optional[int] = None
    product_id: Optional[int] = None
    vendor_name: str = ""
    product_name: str = ""
    sw_version: str = ""
    #: matter-server's reachability flag for the node (node-details ``available``;
    #: also pushed via a node_updated event when it changes). Defaults True so a
    #: server that omits the field is treated as reachable (prior behaviour).
    available: bool = True
    endpoints: list[EndpointInfo] = field(default_factory=list)
    #: Current attribute values keyed by (endpoint, cluster, attribute) — used to
    #: prime Indigo device states on creation (get_node's snapshot), since
    #: matter-server only emits attribute_updated on subsequent *changes*.
    attributes: dict[tuple[int, int, int], Any] = field(default_factory=dict)


def _normalise_attributes(raw_attrs: dict) -> dict[tuple[int, int, int], Any]:
    out: dict[tuple[int, int, int], Any] = {}
    for key, value in raw_attrs.items():
        try:
            out[Protocol.parse_attr_key(key)] = value
        except (ValueError, AttributeError):
            continue
    return out


# Descriptor cluster (0x001D) DeviceTypeList attribute (0): tag-based list of
# structs {"0": deviceType, "1": revision}.
_DESCRIPTOR = 0x001D
_ATTR_DEVICE_TYPE_LIST = 0x0000


def parse_node(raw: dict, suggested_name: str = "") -> NodeInfo:
    """Build a :class:`NodeInfo` from matter-server's node-details object.

    The real node-details object (verified against ws-controller v0.6.2) has NO
    ``endpoints`` key — only a flat ``attributes`` map keyed by
    ``"endpoint/cluster/attribute"``. Endpoints and their cluster sets are
    derived from those attribute paths; device types come from the Descriptor
    cluster's DeviceTypeList.
    """
    attrs = _normalise_attributes(raw.get("attributes") or {})

    clusters_by_endpoint: dict[int, set[int]] = {}
    for (endpoint_id, cluster_id, _attr_id) in attrs:
        clusters_by_endpoint.setdefault(endpoint_id, set()).add(cluster_id)

    endpoints = [
        EndpointInfo(
            endpoint_id=endpoint_id,
            cluster_ids=frozenset(cluster_ids),
            device_types=_device_types(attrs, endpoint_id),
            **_bridge_identity(attrs, endpoint_id),
        )
        for endpoint_id, cluster_ids in sorted(clusters_by_endpoint.items())
    ]

    def basic(attr_id: int, default=None):
        return attrs.get((0, _BASIC_INFO, attr_id), default)

    return NodeInfo(
        node_id=int(raw.get("node_id")),
        suggested_name=suggested_name,
        vendor_id=_opt_int(basic(_ATTR_VENDOR_ID)),
        product_id=_opt_int(basic(_ATTR_PRODUCT_ID)),
        vendor_name=str(basic(_ATTR_VENDOR_NAME, "") or ""),
        product_name=str(basic(_ATTR_PRODUCT_NAME, "") or ""),
        sw_version=str(basic(_ATTR_SW_VERSION_STRING, "") or ""),
        available=bool(raw.get("available", True)),
        endpoints=endpoints,
        attributes=attrs,
    )


def _bridge_identity(attrs: dict, endpoint_id: int) -> dict:
    """Extract per-endpoint BridgedDeviceBasicInformation (0x0039) identity.

    Returns a dict with keys ``node_label``, ``vendor_name``, ``product_name``
    (all str, defaulting to "").  Callers spread it into :class:`EndpointInfo`
    kwargs; for non-bridged endpoints the cluster is absent so all values stay
    empty, which is the correct no-op default.
    """
    def _battr(attr_id: int) -> str:
        val = attrs.get((endpoint_id, CLUSTER_BRIDGED_BASIC, attr_id))
        return str(val or "")

    return {
        "node_label": _battr(_BBRIDGE_ATTR_NODE_LABEL),
        "vendor_name": _battr(_BBRIDGE_ATTR_VENDOR_NAME),
        "product_name": _battr(_BBRIDGE_ATTR_PRODUCT_NAME),
    }


def _device_types(attrs: dict, endpoint_id: int) -> tuple[int, ...]:
    raw_list = attrs.get((endpoint_id, _DESCRIPTOR, _ATTR_DEVICE_TYPE_LIST))
    if not isinstance(raw_list, list):
        return ()
    out: list[int] = []
    for entry in raw_list:
        # tag-based struct: {"0": deviceType, "1": revision}
        if isinstance(entry, dict) and entry.get("0") is not None:
            out.append(int(entry["0"]))
    return tuple(out)


def _opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
