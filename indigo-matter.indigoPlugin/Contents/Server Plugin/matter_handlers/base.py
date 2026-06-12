"""Cluster handler base classes.

Each Matter cluster (or composition of clusters on one endpoint) maps to an
Indigo device via a :class:`ClusterHandler`. This isolates Matter-spec knowledge
into one file per cluster so adding clusters later is a contained change.

``MatterCommand`` is imported from :mod:`protocol` (the single home for the
wire-level command shape); ``IndigoDeviceSpec`` describes an Indigo device to
create. This module is Indigo-free; concrete handlers import ``indigo`` only
where they translate Indigo actions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from protocol import MatterCommand, MatterWrite  # re-exported for handlers/tests

__all__ = ["IndigoDeviceSpec", "MatterCommand", "MatterWrite", "ClusterHandler", "MatterAction"]

#: A handler action is either a cluster-command invoke or an attribute write.
MatterAction = Union[MatterCommand, MatterWrite]


@dataclass
class IndigoDeviceSpec:
    """Description of an Indigo device to create for a Matter endpoint."""
    device_type_id: str
    name: str
    props: dict
    initial_states: dict = field(default_factory=dict)


class ClusterHandler(ABC):
    """Maps a Matter cluster to Indigo device(s), state, and actions."""

    #: Matter cluster id. 0 is a sentinel meaning "subclass forgot to set it";
    #: HandlerRegistry asserts this is non-zero at registration.
    cluster_id: int = 0
    cluster_name: str = ""
    #: Indigo deviceTypeId this handler owns (for action dispatch).
    device_type_id: str = ""
    #: A node-scoped cluster (e.g. PowerSource) lives on a different endpoint
    #: than the device(s) it augments, so dispatch fans its updates out to ALL
    #: of the node's Indigo devices rather than just the one at the event's
    #: endpoint.
    node_scoped: bool = False
    #: Display-capability props every device this handler creates must carry.
    #: Indigo applies Supports*/subtype capabilities via creation props, not
    #: Devices.xml statics, for API-created devices (the colour lesson —
    #: HANDOVER 2026-06-09 item 4; issue #56 for the sensor family). These are
    #: also re-asserted on existing devices by device_sync's reconcile
    #: self-heal, so they must be static truths of the device type, not
    #: per-node capabilities (those belong in create_indigo_devices).
    display_props: dict = {}

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:  # noqa: D401
        """Whether this handler creates the device for *endpoint*.

        Default: primary. Handlers that defer to a richer handler on the same
        endpoint (e.g. OnOff deferring to LevelControl) override this.
        """
        return True

    @abstractmethod
    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        """Build Indigo device spec(s) for an endpoint exposing this cluster."""

    @abstractmethod
    def attributes_to_subscribe(self) -> list[int]:
        """Attribute ids to subscribe to on this cluster."""

    @abstractmethod
    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        """Translate a Matter attribute change to Indigo state updates."""

    def on_node_event(self, indigo_dev: Any, event_id: int, data: Any) -> dict:
        """Translate a Matter cluster event to Indigo state updates.

        Called when the server sends a ``node_event`` frame for a cluster this
        handler owns.  The difference from :meth:`on_attribute_update`:

        - ``on_attribute_update`` fires on *attribute* changes — persistent,
          readable state (OnOff, CurrentLevel, Temperature…).
        - ``on_node_event`` fires on *cluster events* — transient, edge-triggered
          signals such as button presses (GenericSwitch), lock operations
          (DoorLock AccessControlEntryChanged), or occupancy-zone crossings.
          These are not stored in the device's attribute table; they arrive as
          momentary events and must be mapped to Indigo state updates if the
          handler wants them visible to Indigo triggers.

        Default: returns ``{}`` (no-op) so existing handlers need no changes.
        Override in handlers that subscribe to cluster events (e.g.
        :class:`~matter_handlers.generic_switch.GenericSwitchHandler`).
        """
        return {}  # noqa: D401

    @abstractmethod
    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        """Translate an Indigo device action to a Matter command or attribute
        write (or None)."""
