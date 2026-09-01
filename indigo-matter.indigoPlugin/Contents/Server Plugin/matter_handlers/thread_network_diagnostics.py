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
or from ``_prime_states`` replaying a node's cached snapshot one key at a time (in
cache-dict iteration order — device_sync does not control or guarantee which
attribute arrives first). So this handler holds a small per-node cache of the
cluster's own last-seen attribute values (``{node_id: {attribute_id: value}}``,
rebuilt from whatever priming/live traffic has arrived so far, never persisted
across a reload — the same throwaway-cache discipline as ``device_sync``'s own
``_power_coverage``/``_forward_links``), and on every update re-parses the WHOLE
cluster through :func:`thread_mesh.parse_node_diag` before computing
:func:`thread_mesh.summary_states`.

**The priming-order gate (#334 post-review, B2.8).** Because priming replays one
cached attribute at a time, the FIRST attribute to arrive (RoutingRole, say) would
otherwise compute a diag whose NeighborTable has not arrived yet — genuinely
``neighbours_known=False`` (:mod:`thread_mesh`'s own absent-vs-unknown distinction,
B2.1), but for a reason that has nothing to do with the device: the rest of the
snapshot simply has not been replayed yet. That transient "unknown" would land in
the exact same batched Indigo state update as the real numbers a moment later,
which is a false transient a trigger could catch. So this handler withholds every
result — returns ``{}`` — until BOTH RoutingRole and NeighborTable have been SEEN
at least once (their KEY present in the per-node cache, not their VALUE: a live
NeighborTable reading of ``None`` still counts as "seen", exactly like B2.1's own
rule for what "known" means).

Malformed data itself is never this handler's problem to catch: a value
``thread_mesh`` cannot make sense of degrades to no neighbours/no role/no flags
there already (its own module docstring), and this handler relies on
``device_sync``'s own try/except around every handler call (``_prime_states``'s and
``_on_attribute``'s, both wrapping ``handler.on_attribute_update`` individually) for
the "one bad value must not freeze the device" guarantee — which is also where the
warning actually reaches an operator, naming the device and the failing attribute,
something a handler-local logger reaching no configured handler in production could
never do (#334 post-review, B2.6 — this module used to carry its own
``logging.getLogger(__name__)`` and a second, redundant broad ``except`` around the
parse; both are gone, on purpose, not an oversight).

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

**``node_scoped = False``**: same-endpoint like BasicInformation, unlike
PowerSource — ThreadNetworkDiagnostics lives on endpoint 0, the SAME endpoint as
its target ``matterNode`` device, so ordinary same-endpoint dispatch
(``_lookup_for_cluster``) is exactly right and no cross-endpoint fan-out is needed.
"""
from __future__ import annotations

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

    Non-primary (``device_sync`` owns ``matterNode`` creation) and NOT node-scoped:
    this cluster lives on the same endpoint (0) as the device it describes, same
    as ``basic_information.py`` — ordinary same-endpoint dispatch is exactly right,
    unlike ``PowerSourceHandler``'s genuine cross-endpoint case. Read-only:
    ``handle_indigo_action`` always returns ``None`` (ADR-0004).
    """

    cluster_id = CLUSTER_THREAD_DIAG
    cluster_name = "ThreadNetworkDiagnostics"
    device_type_id = "matterNode"
    node_scoped = False

    def __init__(self) -> None:
        # node_id -> {attribute_id: last-seen raw value}. See the module
        # docstring: on_attribute_update only ever sees one changed attribute,
        # so the whole-cluster parse needs this to reassemble the rest.
        #
        # Never evicted (#334 post-review, B2.11): there is no per-node-removal
        # hook a ClusterHandler can register for (searched device_sync.py and
        # matter_handlers/base.py — device_sync evicts its OWN caches, e.g.
        # `_forward_links`/`_reverse_links`, from inside `delete_node`/
        # `node_removed`, but never calls back into a handler instance to do
        # the same). Growth is bounded by the number of DISTINCT Thread node
        # ids this Indigo install has ever seen — decommissioning and
        # recommissioning the same node id simply overwrites its entry, and a
        # genuinely new node id adds one small dict entry, not an unbounded
        # leak. Inventing a hook for this alone was judged not worth adding a
        # new handler-lifecycle mechanism that nothing else in this package
        # needs yet.
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
            # not happen for a real matterNode device, but degrade rather than
            # assume the props are shaped as expected. This narrow except is
            # NOT the broad one B2.6 removed below: it is a shape problem with
            # the DEVICE's own props, not a parse problem with Thread data, and
            # node_endpoint's own contract already names exactly these three
            # exception types.
            return {}

        node_attrs = self._node_attrs.setdefault(node_id, {})
        node_attrs[attribute_id] = value

        # #334 post-review, B2.8: withhold every result until BOTH RoutingRole
        # and NeighborTable have been SEEN at least once (see the module
        # docstring's "priming-order gate" section) — a KEY check, not a value
        # check, so a live NeighborTable reading of None still counts as seen.
        if ATTR_ROUTING_ROLE not in node_attrs or ATTR_NEIGHBOR_TABLE not in node_attrs:
            return {}

        diag = parse_node_diag(
            node_id=node_id,
            name=getattr(indigo_dev, "name", "") or "",
            # "reachable" is device_sync's own liveness state
            # (`_write_node_reachable`), derived from matter-server's node-level
            # `available` flag — NOT from BridgedDeviceBasicInformation, which
            # this node (endpoint 0 of a directly-commissioned node, not a
            # bridged child) does not even carry.
            available=bool(getattr(indigo_dev, "states", {}).get("reachable", True)),
            attributes={
                (0, CLUSTER_THREAD_DIAG, attr): raw
                for attr, raw in node_attrs.items()
            },
        )
        if diag is None:
            return {}
        states = summary_states(diag)

        existing = getattr(indigo_dev, "states", {})
        return {key: state_value for key, state_value in states.items() if key in existing}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # read-only; permanently (ADR-0004)
