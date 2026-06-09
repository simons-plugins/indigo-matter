"""Cluster handler registry.

Holds the handler instances in priority order (Color > Dimmer > Relay > sensors
> thermostat — only Relay/OnOff is wired in M4; later milestones add to this list
with no changes elsewhere). Resolves handlers three ways:

- by endpoint, to decide which Indigo devices an endpoint should produce;
- by cluster id, to dispatch an incoming attribute update;
- by Indigo deviceTypeId, to dispatch an Indigo device action.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec
from .color_control import ColorControlHandler
from .level_control import LevelControlHandler
from .on_off import OnOffHandler
from .sensors import (
    ContactHandler,
    HumidityHandler,
    IlluminanceHandler,
    OccupancyHandler,
    TemperatureHandler,
)
from .thermostat import FanControlHandler, ThermostatHandler


def default_handlers() -> list[ClusterHandler]:
    """Handlers in priority order. Lighting handlers (Color > Dimmer > Relay)
    are mutually exclusive via ``is_primary_for``; sensors are additive;
    Thermostat owns its endpoint and FanControl merges into it."""
    return [
        ColorControlHandler(),
        LevelControlHandler(),
        OnOffHandler(),
        TemperatureHandler(),
        HumidityHandler(),
        OccupancyHandler(),
        ContactHandler(),
        IlluminanceHandler(),
        ThermostatHandler(),
        FanControlHandler(),
    ]


class HandlerRegistry:
    def __init__(self, handlers: Optional[list[ClusterHandler]] = None) -> None:
        self.handlers: list[ClusterHandler] = handlers if handlers is not None else default_handlers()
        for handler in self.handlers:
            if not handler.cluster_id:
                raise ValueError(
                    f"{type(handler).__name__} must set a non-zero cluster_id"
                )
        self._by_cluster = {h.cluster_id: h for h in self.handlers}
        self._by_device_type = {h.device_type_id: h for h in self.handlers if h.device_type_id}

    def handlers_for_endpoint(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        """All Indigo device specs an endpoint should produce, priority-ordered.

        Lighting handlers (Color/Dimmer/Relay) are mutually exclusive via
        ``is_primary_for`` (only one matches a given endpoint), so no break is
        needed; sensor handlers are additive, letting a combined temp+humidity
        endpoint produce two Indigo sensor devices.
        """
        specs: list[IndigoDeviceSpec] = []
        for handler in self.handlers:
            if not endpoint.has(handler.cluster_id):
                continue
            if not handler.is_primary_for(node, endpoint):
                continue
            specs.extend(handler.create_indigo_devices(node, endpoint))
        return specs

    def handler_for_cluster(self, cluster_id: int) -> Optional[ClusterHandler]:
        return self._by_cluster.get(cluster_id)

    def handler_for_device(self, dev: Any) -> Optional[ClusterHandler]:
        return self._by_device_type.get(getattr(dev, "deviceTypeId", None))
