"""PowerSource cluster (0x002F) → battery level on sensor devices.

Battery-powered Matter devices expose the PowerSource cluster, typically on
endpoint 0 (the root endpoint) rather than on the sensor endpoint itself.
Because it lives on a different endpoint from the sensor cluster, it is
node-scoped: dispatch cannot simply target the single device at the event's
endpoint. **Which** devices it does target is the source's own EndpointList
(0x001F) — see :func:`resolve_power_coverage`, the one place that decides it.

Matter spec: BatPercentRemaining (0x000C) uint8, half-percent units 0–200
(divide by 2 to get 0–100 %); nullable.

Like FanControlHandler, this handler is non-primary (creates no Indigo device
of its own) and merges a single state into sibling devices. The batteryLevel
state guard mirrors color_control.py's whiteTemperature guard: a device
created before this feature lacks the state, so we degrade quietly rather
than erroring on every update.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand

CLUSTER_POWER_SOURCE = 0x002F
ATTR_BAT_PERCENT_REMAINING = 0x000C
#: EndpointList — MANDATORY on the cluster (conformance "M" in the pinned
#: @matter/model 0.17.8), and the device's own statement of what it powers.
ATTR_ENDPOINT_LIST = 0x001F


@dataclass(frozen=True)
class PowerCoverage:
    """What each of a node's power sources powers (issue #205)."""

    #: source endpoint → the endpoints it powers (always includes itself).
    by_source: dict[int, frozenset[int]]
    #: the union — every endpoint on the node that has a power source at all.
    covered: frozenset[int]
    #: the sources that answered from EVIDENCE rather than from the fallback.
    #: Diagnostic only: nothing branches on it, but "did the device tell us or
    #: did we guess?" is the first question when a battery lands in the wrong
    #: place, and reconstructing it afterwards is impossible.
    from_endpoint_list: frozenset[int]


def resolve_power_coverage(
    power_source_eps: Iterable[int],
    endpoint_lists: Mapping[int, Any],
    node_endpoint_ids: Iterable[int],
    note: Optional[Callable[..., None]] = None,
) -> PowerCoverage:
    """Decide which endpoints each PowerSource endpoint powers.

    THE authority is ``EndpointList`` (0x001F), core §11.7.7.32:

        A cluster instance with an empty list shall indicate that the power
        source is for the entire node, which includes all endpoints. A cluster
        instance with a non-empty list shall include the endpoint, upon which
        the cluster instance resides.

    The plugin used to reconstruct that rule as a heuristic — "more than one
    PowerSource endpoint on the node ⇒ confine each battery to its own
    endpoint, otherwise fan out node-wide" — at four separate call sites, which
    is what issue #82's cross-contamination bug was a bug IN. Issue #205 makes
    the attribute the authority and demotes the heuristic to a fallback, kept
    (not deleted) because the attribute was only defined at cluster revision 2:
    legacy rev-1 firmware implements the cluster on an application endpoint
    without announcing it anywhere, and the heuristic is still the best guess
    available for those.

    The fallback is applied **per source, not per node**, and that is the whole
    reason this is a loop rather than one node-level decision. The spec's own
    note on legacy implementations calls out bridges by name — "bridge
    implementations support endpoints for bridged devices that have different
    power sources" — so the node most likely to be mixed rev-1/rev-2 is exactly
    the one with several sources. Deciding all-or-nothing there would throw away
    a good answer from the child that DID report one because a sibling did not.

    ``endpoint_lists`` maps a source endpoint to its raw ``EndpointList`` value
    (``None`` when the node never reported one). Both a plain ``None`` and a
    value that is not a list (or a list holding something that is not an
    endpoint number) are treated as ABSENT for routing purposes — the fallback
    heuristic applies to both. Only the malformed case (``raw is not None``) is
    reported through ``note`` at debug, the same degrade-don't-raise idiom as
    ``device_sync._resolve_meter_target``: one non-conformant device must not
    abort a whole node's battery routing. A plain ``None`` is NOT noted — it is
    the expected rev-1 shape (legacy firmware that predates the attribute
    entirely), and noting it would be log noise on every such device.
    """
    sources = sorted(int(ep) for ep in power_source_eps)
    all_endpoints = frozenset(int(ep) for ep in node_endpoint_ids)
    # The pre-#205 heuristic, now only reached by a source that reported no
    # usable EndpointList of its own.
    heuristic = (lambda ep: frozenset({ep})) if len(sources) > 1 else (lambda _ep: all_endpoints)

    by_source: dict[int, frozenset[int]] = {}
    from_endpoint_list: set[int] = set()
    for source_ep in sources:
        raw = endpoint_lists.get(source_ep)
        listed = _parse_endpoint_list(raw)
        if listed is None:
            if raw is not None and note is not None:
                note("power coverage: PowerSource on endpoint %s reported an unusable "
                     "EndpointList (%r) — falling back to the pre-#205 heuristic", source_ep, raw)
            by_source[source_ep] = heuristic(source_ep)
            continue
        from_endpoint_list.add(source_ep)
        # Empty = the whole node. Non-empty = exactly those endpoints; the spec
        # says the list SHALL include the source's own endpoint, so adding it is
        # a no-op for conformant firmware and a defensive repair for the rest.
        by_source[source_ep] = all_endpoints if not listed else (listed | {source_ep})

    covered = frozenset().union(*by_source.values()) if by_source else frozenset()
    return PowerCoverage(
        by_source=by_source,
        covered=covered,
        from_endpoint_list=frozenset(from_endpoint_list),
    )


def _parse_endpoint_list(raw: Any) -> Optional[frozenset[int]]:
    """One raw ``EndpointList`` value as endpoint numbers, or None if unusable.

    None covers both "the node never reported it" and "what it reported is not
    an endpoint list" — the caller separates those only to decide whether to
    ``note`` it, because the ROUTING answer is the same either way.
    """
    if not isinstance(raw, list):
        return None
    try:
        # bool is an int subclass — [True] would silently alias endpoint 1 and
        # be trusted as evidence; reject so it degrades like any other
        # malformed value.
        if any(isinstance(entry, bool) for entry in raw):
            return None
        return frozenset(int(entry) for entry in raw)
    except (TypeError, ValueError):
        return None


class PowerSourceHandler(ClusterHandler):
    """Merges battery level from the PowerSource cluster into sibling devices.

    Node-scoped: the cluster lives on a different endpoint from the devices it
    augments, so dispatch cannot route by the event's endpoint. Which devices
    an update reaches is :func:`resolve_power_coverage`'s answer, cached by
    ``device_sync`` per node (issue #205).
    """
    cluster_id = CLUSTER_POWER_SOURCE
    cluster_name = "PowerSource"
    node_scoped = True

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False  # never creates its own device; merges into siblings

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        # EndpointList is DOCUMENTATION here, not plumbing: matter-server's
        # start_listening streams every attribute with no per-attribute
        # subscription (matter_client.py:103), and the coverage map is rebuilt
        # from the node snapshot on every create_devices pass — the same
        # discipline as _resolve_meter_links. It is listed because this method
        # is where a reader looks to find out what the handler consumes.
        return [ATTR_BAT_PERCENT_REMAINING, ATTR_ENDPOINT_LIST]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        # A live EndpointList event is deliberately a NO-OP here, not an
        # oversight: it is topology, not a reading, and it has no Indigo state
        # to write. The realistic cause of an EndpointList change is a bridge
        # gaining/losing a child, which IS a structure change — and that DOES
        # fire node_updated (see handle_event's EVT_ENDPOINT_ADDED branch in
        # device_sync.py, and the protocol.py note above EVT_ENDPOINT_ADDED
        # citing matter.js PairedNode.ts #triggerNodeStructureChanges), which
        # triggers a fresh create_devices pass that rebuilds the coverage map
        # from the whole snapshot. A pure value rewrite without a structure
        # change would instead wait for the next reconcile — acceptable for
        # topology data.
        if attribute_id != ATTR_BAT_PERCENT_REMAINING or value is None:
            return {}
        # Guard: devices created before this feature lack the batteryLevel state.
        # Mirrors the 'if channel in dev.states' SDK pattern (same as the
        # whiteTemperature guard in color_control.py).
        if "batteryLevel" not in getattr(indigo_dev, "states", {}):
            return {}
        # Matter BatPercentRemaining is in half-percent units (0–200); clamp
        # the result to [0, 100] to defend against out-of-spec values.
        return {"batteryLevel": max(0, min(100, int(value) // 2))}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        return None  # read-only; no writable attributes
