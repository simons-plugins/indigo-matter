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

# BasicInformation cluster (0x0028) attribute ids used for device identity.
_BASIC_INFO = 0x0028
_ATTR_VENDOR_NAME = 0x0001
_ATTR_VENDOR_ID = 0x0002
_ATTR_PRODUCT_NAME = 0x0003
_ATTR_PRODUCT_ID = 0x0004
_ATTR_SW_VERSION_STRING = 0x000A


@dataclass(frozen=True)
class EndpointInfo:
    endpoint_id: int
    cluster_ids: frozenset[int]
    device_types: tuple[int, ...] = ()

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
    endpoints: list[EndpointInfo] = field(default_factory=list)


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
        endpoints=endpoints,
    )


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
