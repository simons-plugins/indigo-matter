"""BasicInformation cluster (0x0028) — node identity, for the synthetic
``matterNode`` device (issue #204, ADR-0008).

This is a handler purely so ``device_sync``'s existing per-cluster machinery
(``_lookup_for_cluster``, ``_prime_states``, ``_on_attribute``) routes
NodeLabel/SoftwareVersionString updates to the node device unchanged — the
node device itself is NOT created here. ``is_primary_for`` returns False and
``create_indigo_devices`` returns ``[]``: ``device_sync._ensure_node_device``/
``_node_spec`` own creation, the same division of labour as the device_sync-
owned ``_unknown_spec``/``_energy_meter_spec`` fallback specs. The reason is
the same in both cases — deciding WHETHER a node device exists needs the
node's full endpoint plan (was anything else created for it? issue #105) and
its ADR-0003 capability evidence, neither of which a per-cluster handler has
access to; only ``device_sync`` sees the whole node.

``device_type_id = "matterNode"`` all the same, because that is what lets
``_lookup_for_cluster``/``handler_for_device`` route BasicInformation updates
and any future node-level action to the right device once it exists.

Cluster 0x0028 was already present in ``device_sync._NON_DEVICE_CLUSTERS``
(endpoint 0's own root, never worth a ``matterUnknown`` placeholder) and
therefore already in ``_recognized_clusters`` before this handler existed.
Registering a handler for it changes neither: ``_NON_DEVICE_CLUSTERS`` is
unioned with ``{h.cluster_id for h in registry.handlers}`` to build
``_recognized_clusters``, so 0x0028 was already a member either way — this
handler only adds subscription/dispatch, not a new "is this cluster
recognized?" answer.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterAction

CLUSTER_BASIC_INFORMATION = 0x0028
# NodeLabel: a user-settable label for the node itself (distinct from an
# Indigo device name — this is what the node calls itself over Matter).
ATTR_NODE_LABEL = 0x0005
# SoftwareVersionString: human-readable firmware version, e.g. "1.2.3".
ATTR_SW_VERSION_STRING = 0x000A


class BasicInformationHandler(ClusterHandler):
    """BasicInformation (0x0028) → the ``matterNode`` device's identity states.

    Non-primary (``device_sync`` owns ``matterNode`` creation) and NOT
    node-scoped: this cluster lives on the same endpoint (0) as the device it
    describes, so ordinary same-endpoint dispatch is exactly right — no
    cross-endpoint fan-out logic is needed, unlike ``PowerSourceHandler``.
    """

    cluster_id = CLUSTER_BASIC_INFORMATION
    cluster_name = "BasicInformation"
    device_type_id = "matterNode"
    node_scoped = False

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False  # device_sync._ensure_node_device owns matterNode creation

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        # Documentation, not plumbing — matter-server's start_listening streams
        # every attribute with no per-attribute subscription (matter_client.py),
        # same note as PowerSourceHandler's. Listed so a reader can see what
        # this handler actually consumes.
        return [ATTR_NODE_LABEL, ATTR_SW_VERSION_STRING]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if value is None:
            return {}
        if attribute_id == ATTR_NODE_LABEL:
            key = "nodeLabel"
        elif attribute_id == ATTR_SW_VERSION_STRING:
            key = "softwareVersion"
        else:
            return {}
        # Guard: a matterNode device created before a future state addition
        # (or, defensively, any other device type this cluster might one day
        # be found dispatching against) lacks the key — same idiom as every
        # other merge-in/state-guarded handler in this package.
        if key not in getattr(indigo_dev, "states", {}):
            return {}
        return {key: str(value)}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # read-only; node-level settings are future work (ADR-0008)
