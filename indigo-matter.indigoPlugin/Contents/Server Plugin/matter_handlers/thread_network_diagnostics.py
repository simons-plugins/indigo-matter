"""ThreadNetworkDiagnostics cluster (0x0035) → the ``matterNode`` Thread states (#334).

Exists as a handler for the same reason ``basic_information.py`` does: it is not
what creates the synthetic ``matterNode`` device (``device_sync`` alone decides
whether one exists — ADR-0008), it exists so device_sync's ORDINARY per-cluster
dispatch (``_lookup_for_cluster``/``_prime_states``/``_on_attribute``) routes
ThreadNetworkDiagnostics attribute updates onto that device at all. ``is_primary_for``
is False and ``create_indigo_devices`` returns ``[]`` for exactly that reason.
Like every diagnostics handler in this package, it is permanently read-only
(ADR-0004): ``handle_indigo_action`` returns ``None`` unconditionally, and cluster
0x0035 has no writable attribute for it to drive (verified against
``writable_attributes.WRITABLE_ATTRIBUTES``, which carries no key for 0x0035 at all).

**Why this handler keeps its own cache.** The four states it writes
(``threadRole``/``threadNeighbourCount``/``threadLinkRssi``/``threadHealth``, ADR-0008
— "deliberately short") are not one attribute each; they are :func:`thread_mesh.
summary_states` of the WHOLE cluster, which needs RoutingRole, NeighborTable,
RouteTable, LeaderRouterId and ParentChangeCount together (the last gated, per
ADR-0003, on the cluster's own AttributeList). But ``on_attribute_update`` — like
every handler's, by device_sync's own contract — is called with exactly ONE changed
attribute at a time, whether that call comes from a live ``attribute_updated`` event
or from ``_prime_states`` replaying a node's cached snapshot one key at a time. So
this handler holds a small per-node cache of the cluster's own last-seen attribute
values (``{node_id: {attribute_id: value}}``, rebuilt from whatever priming/live
traffic has arrived so far, never persisted across a reload — the same throwaway-cache
discipline as ``device_sync``'s own ``_power_coverage``/``_forward_links``), and on
every update re-parses the WHOLE cluster through :func:`thread_mesh.parse_node_diag`
before computing :func:`thread_mesh.summary_states`. Never raises on malformed data:
a value ``thread_mesh`` cannot make sense of degrades to no neighbours/no role/no
flags there already (its own module docstring), and this handler wraps the parse in
a broad ``except`` besides, logging at debug and returning ``{}`` — the same
"one bad value must not freeze the device" posture as ``device_sync._on_attribute``'s
own try/except around every handler call, just one layer closer to the data.

**Which attributes are subscribed.** Only what the four states actually consume,
per ``thread_mesh.health_flags``: RoutingRole (role_name, detached, is_router,
single_neighbour), NeighborTable (neighbour count, weak_link, high_frame_error,
the parent for a non-router's ``threadLinkRssi``), RouteTable + LeaderRouterId
(far_from_leader — the one health check that needs a second attribute pair beyond
NeighborTable to compute a path cost to the leader), ParentChangeCount
(unstable_parent), and the cluster's own AttributeList (65531) — not because a
state reads it directly, but because ``thread_mesh.parse_node_diag`` gates
ParentChangeCount behind it (ADR-0003): without the AttributeList cached, a node's
counters dict stays empty forever regardless of how many times attribute 21 itself
arrives, and ``unstable_parent`` could never fire.

**Why ``node_scoped = True`` despite the cluster living on the SAME endpoint (0) as
its target device** — unlike PowerSource's genuine cross-endpoint case, which is
what ``node_scoped`` was built for. ``_on_attribute``'s node-scoped fan-out routes
through ``_battery_targets``, PowerSource's own coverage cache, which has no
special knowledge of Thread at all — but that fan-out's target set is guaranteed to
always include the event's own endpoint (``_battery_targets`` either returns
``None``, meaning node-wide, or a set built by unioning in the queried endpoint
itself), so this cluster's ep-0 events always still reach the ep-0 ``matterNode``
device, on every node shape (no PowerSource, one, or several). The cost is a few
harmless extra dispatch calls to sibling devices on a battery-covered node, each
dropped immediately by the ``key in indigo_dev.states`` guard below — cheaper than
adding a second fan-out seam to device_sync for one handler.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from thread_mesh import (
    ATTR_ATTRIBUTE_LIST,
    ATTR_LEADER_ROUTER_ID,
    ATTR_NEIGHBOR_TABLE,
    ATTR_PARENT_CHANGE_COUNT,
    ATTR_ROUTE_TABLE,
    ATTR_ROUTING_ROLE,
    CLUSTER_THREAD_DIAG,
    parse_node_diag,
    summary_states,
)

from .base import ClusterHandler, IndigoDeviceSpec, MatterAction, node_endpoint

#: ``logging.getLogger(__name__)`` reaches no handler in production (same note as
#: export_catalog.py's ``_LOG``) — fine here, since "never raise" only asks for a
#: debug-level breadcrumb, not a guaranteed Event Log line (this is diagnostics
#: data with no write path, so nothing downstream is actually acting on silence).
_LOG = logging.getLogger(__name__)

#: Everything ``thread_mesh.summary_states``/``health_flags`` read on this cluster —
#: see the module docstring for what each one feeds. Not every ThreadNetworkDiagnostics
#: attribute matter-server may report; deliberately narrower.
_SUBSCRIBED_ATTRS: frozenset = frozenset({
    ATTR_ROUTING_ROLE,
    ATTR_NEIGHBOR_TABLE,
    ATTR_ROUTE_TABLE,
    ATTR_LEADER_ROUTER_ID,
    ATTR_PARENT_CHANGE_COUNT,
    ATTR_ATTRIBUTE_LIST,
})


class ThreadNetworkDiagnosticsHandler(ClusterHandler):
    """ThreadNetworkDiagnostics (0x0035) → the ``matterNode`` device's Thread states.

    Non-primary (``device_sync`` owns ``matterNode`` creation) and node-scoped —
    see the module docstring for why that flag is set despite the cluster sharing
    its target device's own endpoint. Read-only: ``handle_indigo_action`` always
    returns ``None`` (ADR-0004).
    """

    cluster_id = CLUSTER_THREAD_DIAG
    cluster_name = "ThreadNetworkDiagnostics"
    device_type_id = "matterNode"
    node_scoped = True

    def __init__(self) -> None:
        # node_id -> {attribute_id: last-seen raw value}. See the module
        # docstring: on_attribute_update only ever sees one changed attribute,
        # so the whole-cluster parse needs this to reassemble the rest.
        self._node_attrs: dict[int, dict[int, Any]] = {}

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False  # device_sync._ensure_node_device owns matterNode creation

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        return sorted(_SUBSCRIBED_ATTRS)

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id not in _SUBSCRIBED_ATTRS:
            return {}
        try:
            node_id, _endpoint_id = node_endpoint(indigo_dev)
        except (KeyError, TypeError, ValueError):
            # A device with no (or unparsable) nodeId/endpointId props — should
            # not happen for a real matterNode device, but this handler can be
            # reached (via node_scoped fan-out) against ANY device on the node,
            # so degrade rather than assume the props are shaped as expected.
            _LOG.debug("thread diag update for a device with no usable nodeId/endpointId props")
            return {}

        node_attrs = self._node_attrs.setdefault(node_id, {})
        node_attrs[attribute_id] = value

        try:
            diag = parse_node_diag(
                node_id=node_id,
                name=getattr(indigo_dev, "name", "") or "",
                # "reachable" is device_sync's own BridgedDeviceBasicInformation-
                # derived liveness state (already declared on matterNode) — the
                # closest thing to "is this node currently available" this
                # handler has, without threading a second signal through.
                available=bool(getattr(indigo_dev, "states", {}).get("reachable", True)),
                attributes={
                    (0, CLUSTER_THREAD_DIAG, attr): raw
                    for attr, raw in node_attrs.items()
                },
            )
            if diag is None:
                return {}
            states = summary_states(diag)
        except Exception as exc:  # pylint: disable=broad-except
            # Never raise on malformed data: thread_mesh already tolerates most
            # of it internally (see its own module docstring), this is the outer
            # belt for whatever it doesn't — one bad value must not freeze the
            # device's Thread states, same posture as device_sync's own
            # try/except around every handler call, just closer to the data.
            _LOG.debug("could not parse Thread diagnostics for node %s: %s", node_id, exc)
            return {}

        existing = getattr(indigo_dev, "states", {})
        return {key: state_value for key, state_value in states.items() if key in existing}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # read-only; permanently (ADR-0004)
