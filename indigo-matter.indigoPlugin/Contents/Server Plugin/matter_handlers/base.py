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
from typing import Any, Optional

from protocol import MatterCommand  # re-exported for handlers/tests

__all__ = ["IndigoDeviceSpec", "MatterCommand", "ClusterHandler"]


@dataclass
class IndigoDeviceSpec:
    """Description of an Indigo device to create for a Matter endpoint."""
    device_type_id: str
    name: str
    props: dict
    initial_states: dict = field(default_factory=dict)


class ClusterHandler(ABC):
    """Maps a Matter cluster to Indigo device(s), state, and actions."""

    cluster_id: int
    cluster_name: str
    #: Indigo deviceTypeId this handler owns (for action dispatch).
    device_type_id: str = ""

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

    @abstractmethod
    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        """Translate an Indigo device action to a Matter command (or None)."""
