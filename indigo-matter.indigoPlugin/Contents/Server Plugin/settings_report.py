"""What a Matter device exposes that this plugin does not offer (issue #191).

Two read-only diagnostics, one mechanism:

* **The settable-attribute report** (deliverable B) — for each device, the
  writable Matter attributes it implements that the plugin has no setting for.
  Fired automatically once per device and available on demand from the Matter
  menu, so a user can paste it to a maintainer without knowing what Matter is.
* **The attribute explorer** (deliverable A) — a labelled dump of what a chosen
  node/endpoint/cluster actually holds, for someone who does know.

**Why this exists.** Adding support for a device's settings requires knowing
what it exposes, and until now the only way to find out was to dump ``get_node``
by hand and read the JSON. That is how the FP300 survey was done and it is not
repeatable by anyone else — you cannot buy every device, but users own them.

The sharper argument is issue #190's, which this module answers directly: that
issue's device attribution was **wrong** — it named the wrong plug as
implementing OnOff's Lighting feature, and the error survived into the issue,
into the test fixtures, and into a release before real hardware caught it. A
machine-generated report would not have made that mistake. Everything here is
derived from the device's own AttributeList and a generated table; nothing is
transcribed by a human.

**Read-only, permanently.** There is no write path here and one is not coming
(decided 2026-08-11). A generic attribute writer is unvalidated by construction
— the whole point of ``matter_handlers.settings`` is that every write is
bounds-checked against a declared strategy and verified by read-back — it is
device-global on devices that are typically joined to two or more ecosystems,
and it adds no capability, because the settings dialog already covers every
write for which a validated bounds strategy exists. If a write is ever needed
the answer is a new ``DeviceSetting`` declaration, never a generic writer.

**Output goes to the event log**, not to a dialog. This is forced rather than
chosen: Indigo has no per-field ConfigUI change callback, and the only dynamic
mechanism (``<List dynamicReload="true">``) regenerates a list's *options*. So a
menu dialog physically cannot display a result — the same conclusion the pairing
codes reached (PRD §3.8), for the same reason.

Indigo-free and matter-server-free: everything takes a :class:`NodeInfo` and
plain callables, so the whole surface is unit-testable without hardware.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from matter_handlers.settings import ATTR_ATTRIBUTE_LIST, SETTINGS, implements
from matter_handlers.writable_attributes import (
    ATTRIBUTE_NAMES,
    CLUSTER_NAMES,
    COMMAND_FIELD_ATTRIBUTES,
    MODEL_VERSION,
    WRITABLE_ATTRIBUTES,
)
from matter_model import NodeInfo, node_id_to_str

__all__ = [
    "SettableAttribute", "EndpointSurvey", "NodeSurvey", "SurveyLog",
    "survey_node", "report_lines", "explore_lines",
    "cluster_label", "attribute_label", "GLOBAL_ATTRIBUTES",
    "INFRASTRUCTURE_CLUSTERS", "DRIVEN_ATTRIBUTES", "COMMAND_FIELD_ATTRIBUTES",
    "MODEL_VERSION",
]

#: The global attributes every cluster carries. Not in the generated table —
#: the model defines them once on a base element rather than per cluster — so
#: they are named here. Spec-fixed ids; they do not move between revisions.
GLOBAL_ATTRIBUTES: dict[int, str] = {
    0xFFF8: "GeneratedCommandList",
    0xFFF9: "AcceptedCommandList",
    0xFFFA: "EventList",
    0xFFFB: "AttributeList",
    0xFFFC: "FeatureMap",
    0xFFFD: "ClusterRevision",
}

#: Clusters whose writable attributes are Matter PLUMBING, never a device
#: setting a user would ask for or a maintainer would implement. Excluded from
#: the report — not from the explorer, which is deliberately unfiltered.
#:
#: Judgement, and worth stating so it can be argued with: the test is "would a
#: ``DeviceSetting`` for this ever be the right answer?". For a commissioning
#: breadcrumb, a network interface toggle, or the Thread network directory's
#: preferred PAN id it plainly would not — those belong to the fabric, not to
#: the device's behaviour. Reporting them would bury the two or three lines that
#: matter under a wall of noise, which is the failure mode this report has to
#: avoid: an INFO nobody reads is worse than no INFO.
#:
#: ``Identify`` is here for a different reason. IdentifyTime is writable, but it
#: is a momentary command in attribute clothing ("blink for N seconds") with no
#: persistent setting behind it.
#: Identify and GeneralCommissioning are now covered TWICE — their only writable
#: attribute is also a command field, so ``COMMAND_FIELD_ATTRIBUTES`` excludes
#: them too. Deliberately kept here anyway: this list answers "is the cluster
#: infrastructure?", which stays the right exclusion if either ever gains a
#: writable attribute that is not a command parameter.
INFRASTRUCTURE_CLUSTERS: frozenset = frozenset({
    0x0003,  # Identify — IdentifyTime is a momentary command, not a setting
    0x0028,  # BasicInformation — NodeLabel/Location/LocalConfigDisabled
    0x002B,  # LocalizationConfiguration — ActiveLocale
    0x002C,  # TimeFormatLocalization — HourFormat/ActiveCalendarType
    0x0030,  # GeneralCommissioning — Breadcrumb
    0x0031,  # NetworkCommissioning — InterfaceEnabled
    0x0453,  # ThreadNetworkDirectory — PreferredExtendedPanId
})

#: Writable attributes the plugin ALREADY drives, as ordinary Indigo controls
#: rather than as settings. Reporting these as gaps would send a user to file an
#: issue for a feature that shipped — exactly the support noise an automatic
#: INFO has to earn its place against.
#:
#: Hand-maintained, because there is no way to derive it: a handler builds a
#: :class:`~protocol.MatterWrite` at call time from an Indigo action, and only
#: running one would say which attribute it targets. Kept honest by a parity
#: test that greps every handler for ``MatterWrite(`` and asserts each pair is
#: either declared as a ``DeviceSetting`` or listed here.
DRIVEN_ATTRIBUTES: frozenset = frozenset({
    (0x0201, 0x0011),  # Thermostat OccupiedCoolingSetpoint — cool setpoint action
    (0x0201, 0x0012),  # Thermostat OccupiedHeatingSetpoint — heat setpoint action
    (0x0201, 0x001C),  # Thermostat SystemMode — HVAC mode action
    (0x0202, 0x0000),  # FanControl FanMode — fan on/off/mode
    (0x0202, 0x0002),  # FanControl PercentSetting — fan speed (dimmer-style)
})

#: Longest rendered attribute value the explorer prints before truncating. A
#: node's AttributeList or a camera's Viewport struct can be hundreds of
#: characters, and an event log line that wraps forty times is unreadable — but
#: silently dropping the tail would make the dump untrustworthy, so a truncated
#: value says so.
MAX_VALUE_CHARS = 220


def cluster_label(cluster: int) -> str:
    """``"OnOff (0x0006)"`` — name if the model knows it, id regardless.

    The id is always shown, never only the name: it is what a maintainer needs
    to look the cluster up, and an unrecognised cluster (a manufacturer
    extension, or one added after the pinned model) has nothing else.
    """
    name = CLUSTER_NAMES.get(int(cluster))
    return f"{name} (0x{int(cluster):04X})" if name else f"cluster 0x{int(cluster):04X}"


def attribute_label(cluster: int, attribute: int) -> str:
    """``"StartUpOnOff (0x4003)"`` — the attribute's model name and id."""
    attribute = int(attribute)
    name = (ATTRIBUTE_NAMES.get(int(cluster), {}).get(attribute)
            or GLOBAL_ATTRIBUTES.get(attribute))
    return f"{name} (0x{attribute:04X})" if name else f"attribute 0x{attribute:04X}"


@dataclass(frozen=True)
class SettableAttribute:
    """One writable attribute a device implements and the plugin does not offer."""

    cluster: int
    attribute: int

    def describe(self) -> str:
        return f"{attribute_label(self.cluster, self.attribute)} on {cluster_label(self.cluster)}"


@dataclass(frozen=True)
class EndpointSurvey:
    """One endpoint's settable attributes, with the device name if it has one."""

    endpoint: int
    device_names: tuple = ()
    settable: tuple = ()

    def title(self) -> str:
        if self.device_names:
            return f"{', '.join(self.device_names)} (endpoint {self.endpoint})"
        return f"endpoint {self.endpoint}"


@dataclass(frozen=True)
class NodeSurvey:
    """Everything one physical Matter device exposes that the plugin does not.

    Node-level rather than per Indigo device on purpose: the thing a user pastes
    to a maintainer is "here is my plug", and a node's endpoints are one
    product. It also makes the once-per-device bookkeeping match the object it
    is about — a node id, not an Indigo device id that a user can delete and
    recreate at will.
    """

    node_id: int
    vendor: str = ""
    product: str = ""
    firmware: str = ""
    endpoints: tuple = ()

    @property
    def count(self) -> int:
        return sum(len(ep.settable) for ep in self.endpoints)

    def identity(self) -> str:
        """``"Aqara FP300 firmware 1.2.3 (node 0x38)"`` — what a maintainer needs.

        Firmware is named because issue #186 established that behaviour here is
        firmware-specific: the same product on two releases is, for this
        purpose, two devices.
        """
        parts = [p for p in (self.vendor, self.product) if p]
        who = " ".join(parts) if parts else "unknown device"
        version = f" firmware {self.firmware}" if self.firmware else ""
        return f"{who}{version} (node {node_id_to_str(self.node_id)})"

    def fingerprint(self) -> str:
        """The value stored to decide whether this node has been reported.

        Firmware AND the settable set, because either moving is a genuine change
        of answer. Firmware alone would miss a device whose first informative
        snapshot arrived before its interview finished; the set alone would miss
        a firmware update that changed nothing it reports but everything about
        how it behaves (issue #186's whole finding).
        """
        pairs = sorted((ep.endpoint, s.cluster, s.attribute)
                       for ep in self.endpoints for s in ep.settable)
        return f"{self.firmware}#{json.dumps(pairs, separators=(',', ':'))}"


def survey_node(node: NodeInfo,
                offered: Optional[Callable[[int], Iterable]] = None) -> NodeSurvey:
    """Which writable attributes this node implements that the plugin does not offer.

    ``offered(endpoint)`` returns the ``(cluster, attribute)`` pairs the plugin
    already offers as settings THERE — which is endpoint-specific because a
    setting is declared against Indigo device types, and what type an endpoint
    became is the only thing that decides whether its field exists. Absent, no
    setting is treated as offered, which is the right default for a node with no
    Indigo devices at all (an empty bridge, or a menu run before reconcile).

    The evidence is the device's own **AttributeList** (0xFFFB) per cluster,
    never the attribute values in the snapshot. A value present in ``get_node``
    is not proof the device implements the attribute, and — the other way round
    — an implemented attribute the device has not reported a value for is still
    a gap worth naming. Clusters with no AttributeList captured contribute
    nothing rather than being guessed at, so a partial interview under-reports
    instead of inventing.

    Three exclusions, all of them things that ARE writable and still are not
    gaps: infrastructure clusters, attributes the plugin already drives as
    ordinary controls, and **command parameters** — writable attributes that are
    a field of a command on their own cluster, which the command sets and which
    mean nothing pre-set (#197, ADR-0005). The explorer still shows all three.
    """
    endpoints: list[EndpointSurvey] = []
    for endpoint in sorted({ep for (ep, _c, _a) in node.attributes}):
        already = set(offered(endpoint)) if offered is not None else set()
        settable: list[SettableAttribute] = []
        for cluster in sorted({c for (ep, c, _a) in node.attributes if ep == endpoint}):
            if cluster in INFRASTRUCTURE_CLUSTERS:
                continue
            writable = WRITABLE_ATTRIBUTES.get(cluster)
            if not writable:
                continue
            listed = node.attributes.get((endpoint, cluster, ATTR_ATTRIBUTE_LIST))
            for attribute in sorted(writable):
                if implements(listed, attribute) is not True:
                    continue
                if (cluster, attribute) in DRIVEN_ATTRIBUTES:
                    continue
                if attribute in COMMAND_FIELD_ATTRIBUTES.get(cluster, ()):
                    continue
                if (cluster, attribute) in already:
                    continue
                settable.append(SettableAttribute(cluster, attribute))
        if settable:
            endpoints.append(EndpointSurvey(endpoint, (), tuple(settable)))
    return NodeSurvey(
        node_id=int(node.node_id),
        vendor=node.vendor_name or "",
        product=node.product_name or "",
        firmware=node.sw_version or "",
        endpoints=tuple(endpoints),
    )


def name_endpoints(survey: NodeSurvey,
                   names: Callable[[int], Iterable]) -> NodeSurvey:
    """Attach Indigo device names to a survey's endpoints.

    Separate from :func:`survey_node` because looking a name up means touching
    Indigo, and the survey itself must stay usable without it — the on-demand
    menu path runs against a node that may have no Indigo devices at all.
    """
    return NodeSurvey(
        node_id=survey.node_id, vendor=survey.vendor, product=survey.product,
        firmware=survey.firmware,
        endpoints=tuple(
            EndpointSurvey(ep.endpoint, tuple(names(ep.endpoint)), ep.settable)
            for ep in survey.endpoints
        ),
    )


def report_lines(survey: NodeSurvey) -> list[str]:
    """The event-log block for one node, or ``[]`` when there is nothing to say.

    Worded as information, never as a fault. This fires unprompted at
    commissioning on hardware that is working perfectly, and a line that reads
    like an error generates support traffic for a non-problem — so it says what
    the device has, says plainly that nothing is wrong, and makes the one useful
    action (send it to a maintainer) obvious.
    """
    if not survey.endpoints:
        return []
    plural = "attribute" if survey.count == 1 else "attributes"
    lines = [
        f"{survey.identity()} implements {survey.count} settable Matter {plural} "
        f"that this plugin does not offer yet. Nothing is wrong — the device works "
        f"normally; this is a list of settings that COULD be added.",
    ]
    for endpoint in survey.endpoints:
        lines.append(f"  {endpoint.title()}:")
        for item in endpoint.settable:
            lines.append(f"    - {item.describe()}")
    lines.append(
        "  To have any of these added, please open an issue at "
        "github.com/simons-plugins/indigo-matter/issues and paste these lines. "
        "This message appears once per device.")
    return lines


def render_value(value: Any) -> str:
    """One attribute value as a single log-safe line.

    JSON rather than ``repr``: matter-server's structs arrive index-keyed
    (``{"0": 1, "1": 300}``) and a maintainer reading the dump needs the wire
    shape, which is the one thing a prettified rendering would destroy — coding
    against a prose rendering of exactly this is what shipped a settings field
    that never appeared (issue #186).
    """
    try:
        text = json.dumps(value, separators=(", ", ": "), default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > MAX_VALUE_CHARS:
        return f"{text[:MAX_VALUE_CHARS]}… (truncated, {len(text)} chars)"
    return text


def explore_lines(node: NodeInfo, endpoint: Optional[int] = None,
                  cluster: Optional[int] = None) -> list[str]:
    """A labelled dump of what a node holds — deliverable A's whole output.

    ``endpoint``/``cluster`` of None mean "all". Values come from the node
    snapshot, which is matter-server's **cache**: complete and instant, and for
    exploring a device that is the right source — one call returns everything
    with no multi-second round trip per attribute to a sleepy Thread device. It
    is the wrong source for a value a decision depends on, which is why the
    menu offers a separate live single-attribute read for that and this does
    not pretend to be one. Each block says so.

    Every attribute is shown, including the infrastructure clusters and command
    parameters the report filters out: this is the power-user surface, and hiding
    things from a diagnostic is how a diagnostic stops being trusted. A command
    parameter is labelled as one instead, which is more use than omitting it —
    it answers the question the omission would have raised.
    """
    wanted = {
        (ep, cl, at): value for (ep, cl, at), value in node.attributes.items()
        if (endpoint is None or ep == endpoint) and (cluster is None or cl == cluster)
    }
    header = [
        f"Matter attribute dump — {_explore_scope(endpoint, cluster)} of "
        f"{_node_identity(node)}.",
        "  Values are matter-server's cached snapshot: complete and instant, but an "
        "unsubscribed reading can be stale. Use 'read one attribute live' to confirm a "
        "specific value.",
    ]
    if not wanted:
        return header + ["  (nothing reported here — check the endpoint and cluster, or "
                         "the device may not have finished its interview)"]
    lines = list(header)
    for ep in sorted({e for (e, _c, _a) in wanted}):
        lines.append(f"  endpoint {ep}:")
        for cl in sorted({c for (e, c, _a) in wanted if e == ep}):
            writable = WRITABLE_ATTRIBUTES.get(cl, frozenset())
            parameters = COMMAND_FIELD_ATTRIBUTES.get(cl, frozenset())
            lines.append(f"    {cluster_label(cl)}:")
            for at in sorted(a for (e, c, a) in wanted if e == ep and c == cl):
                if at in parameters:
                    # Shown, and shown as what it is. The report filters these
                    # out; hiding them HERE would leave a user who read the spec
                    # and expected the attribute wondering if the dump was
                    # broken — the report's job is to be quiet, the explorer's
                    # is to be complete (ADR-0004).
                    flag = " [writable, command parameter]"
                else:
                    flag = " [writable]" if at in writable else ""
                lines.append(f"      {attribute_label(cl, at)}{flag} = "
                             f"{render_value(wanted[(ep, cl, at)])}")
    return lines


def _explore_scope(endpoint: Optional[int], cluster: Optional[int]) -> str:
    where = "all endpoints" if endpoint is None else f"endpoint {endpoint}"
    what = "all clusters" if cluster is None else cluster_label(cluster)
    return f"{what} on {where}"


def _node_identity(node: NodeInfo) -> str:
    parts = [p for p in (node.vendor_name, node.product_name) if p]
    who = " ".join(parts) if parts else "unknown device"
    version = f" firmware {node.sw_version}" if node.sw_version else ""
    return f"{who}{version} (node {node_id_to_str(node.node_id)})"


class SurveyLog:
    """Which nodes have already been reported, and with what answer.

    **Why this is a plugin PREF and not a device prop.** The obvious home is the
    Indigo device — props are pruned for free when the device is deleted. But
    the report is about a NODE, and a node is not an Indigo device: a plug is one
    node and one device, an FP300 is one node and four. Keying by device would
    either report the same physical unit up to four times or pick one device
    arbitrarily and lose the record when a user deletes that one. Stamping a prop
    also costs a ``replacePluginPropsOnServer`` per device, which restarts device
    communication (``didDeviceCommPropertyChange`` defaults to "any prop
    changed") — a real cost for bookkeeping that changes nothing about the
    device.

    So: one JSON string in ``pluginPrefs``, keyed by node id, the same shape
    ``export_store`` uses for the allow-list. It is bounded by the number of
    commissioned nodes and pruned on decommission (``node_removed``), which is
    the answer to the growth objection.

    A blob that will not parse starts empty rather than raising. The cost of
    being wrong is one duplicate INFO block; the cost of raising is a reconcile
    pass that dies on its bookkeeping.
    """

    def __init__(self, save: Optional[Callable[[str], None]] = None) -> None:
        self._seen: dict[str, str] = {}
        self._save = save
        self._lock = threading.RLock()

    def load(self, blob: Any) -> None:
        """Replace the log from a stored JSON string (or anything unusable)."""
        parsed: dict[str, str] = {}
        if isinstance(blob, str) and blob.strip():
            try:
                raw = json.loads(blob)
                if isinstance(raw, dict):
                    parsed = {str(k): str(v) for k, v in raw.items()}
            except (TypeError, ValueError):
                parsed = {}
        with self._lock:
            self._seen = parsed

    def to_json(self) -> str:
        with self._lock:
            return json.dumps(self._seen, separators=(",", ":"), sort_keys=True)

    def should_report(self, node_id: Any, fingerprint: str) -> bool:
        """Has this node's answer changed since it was last reported?"""
        with self._lock:
            return self._seen.get(str(int(node_id))) != fingerprint

    def record(self, node_id: Any, fingerprint: str) -> None:
        """Remember that this answer has been reported, and persist."""
        with self._lock:
            self._seen[str(int(node_id))] = fingerprint
        self._persist()

    def forget(self, node_id: Any) -> None:
        """Drop a decommissioned node, so a re-commission reports afresh.

        Node ids are assigned at commissioning and re-used across a
        decommission/recommission cycle, so without this a genuinely different
        device could inherit the old one's "already reported" mark.
        """
        with self._lock:
            existed = self._seen.pop(str(int(node_id)), None) is not None
        if existed:
            self._persist()

    def _persist(self) -> None:
        if self._save is None:
            return
        try:
            self._save(self.to_json())
        except Exception:  # noqa: BLE001 - bookkeeping must never sink a reconcile
            # Deliberately silent: the caller supplies the save hook and logs
            # there if it wants to. A failed save costs one repeated INFO on the
            # next start, which is a strictly better outcome than an exception
            # escaping into create_devices.
            pass


def declared_pairs() -> set:
    """Every ``(cluster, attribute)`` the settings registry declares, any type.

    Used by the parity test, and by callers with no device type to hand.
    """
    return {(s.cluster, s.attribute) for s in SETTINGS}
