"""E3: the outbound export engine (`export_bridge`).

What is pinned here, and why each earns its place:

* **The endpoint provider re-classifies** — the allow-list is a user declaration
  made in the past, so a device that has since been deleted, disabled, taken
  over by this plugin, or re-typed must be skipped with a warning rather than
  sent as a stale spec (the E2 handover's standing requirement);
* **a role this version cannot bridge is skipped, not sent** — an unknown role
  fails the WHOLE attach on the node (E3a), so one un-bridgeable export would
  silently un-export every working one. E4 made the handler table total over the
  v1 enum, so the tests that exercise this path now *remove* a handler
  (:func:`unbridgeable_role`) rather than naming a role that has none: the
  behaviour still has to work, because it is what protects an old plugin from an
  allow-list written by a newer one;
* **the attach deadline scales with the endpoint count** — the node paces bulk
  removals ~100ms apart, so a fixed 8s deadline times out on exactly the large
  databases that most need export to work;
* **the client exists only while something is exported** (XG5), and emptying the
  allow-list goes out as the deliberate §3.1 ``replace_all`` attach, not as a
  disconnect;
* **nothing is awaited on Indigo's thread** — every push is fire-and-forget, and
  a failed one still reaches the log (§3.4).

References to ``§N`` are ``docs/BRIDGE_PROTOCOL.md``.
"""
from __future__ import annotations

import importlib

import pytest

import bridge_client
import bridge_protocol
import export_catalog
from export_store import ExportEntry, ExportStore

from conftest import load_bridge_frames
from fakes import (
    OTHER_PLUGIN_ID,
    DimmerDevice,
    FakeBridgeClient,
    FakeHealthDevice,
    FakeIndigoDevices,
    InlineExecutor,
    RecordingRuntime,
    RelayDevice,
    SprinklerDevice,
)

FRAMES = load_bridge_frames()
OURS = export_catalog.DEFAULT_PLUGIN_ID


@pytest.fixture
def bridge_mod(mock_indigo_base):
    """`export_bridge` (and the handlers it uses) bound to a mocked ``indigo``."""
    import export_handlers
    import export_bridge as module
    importlib.reload(export_handlers)
    importlib.reload(module)
    return module


class Harness:
    """An ExportBridge wired to fakes, plus the knobs the tests need."""

    def __init__(self, module, mock_logger, devices, entries=()):
        self.logger = mock_logger
        self.prefs: dict = {}
        self.devices = devices
        self.store = ExportStore(lambda: self.prefs, mock_logger)
        for entry in entries:
            self.store.upsert(entry)
        self.runtime = RecordingRuntime()
        self.clients: list[FakeBridgeClient] = []
        self.executor = InlineExecutor()
        self.saves = 0
        #: The `matterBridgeHealth` doubles this harness knows about, oldest
        #: first — issue #286. A test seeds this directly (append BEFORE the
        #: first churn signal) to model a device that already existed —
        #: from a previous plugin session, or hand-created — rather than one
        #: this harness's own factory made.
        self.health_devices: list[FakeHealthDevice] = []
        #: Raise instead of creating one, for the "creation fails" tests.
        self.health_device_fail = False
        #: Return ``None`` without raising — the "factory is silently a
        #: no-op" case (review finding 5).
        self.health_device_return_none = False
        #: How many times the find-only seam was actually CALLED (issue #288
        #: review finding C's pin: the throttled unchecked path must scan at
        #: most once per session).
        self.health_device_finder_calls = 0
        #: Raise this instead of answering, for the "a read failure must not
        #: propagate out of health_tick" pin (review finding C).
        self.health_device_finder_raises: Exception | None = None
        self.bridge = module.ExportBridge(
            self.store, self.runtime, mock_logger, lambda: self.prefs,
            plugin_version="2026.7.28", plugin_id=OURS,
            device_getter=self._device,
            client_factory=self._client,
            save_prefs=self._save_prefs,
            executor_factory=lambda: self.executor,
            health_device_finder=self._find_health_device,
            health_device_factory=self._create_health_device,
        )

    def _save_prefs(self) -> None:
        self.saves += 1

    def _device(self, device_id):
        try:
            return self.devices[device_id]
        except KeyError:
            return None

    def _find_health_device(self):
        """The find-only seam — NEVER creates. Only ever answers with a
        device this harness already knows about (review finding 1)."""
        self.health_device_finder_calls += 1
        if self.health_device_finder_raises is not None:
            raise self.health_device_finder_raises
        return self.health_devices[0] if self.health_devices else None

    def _create_health_device(self):
        if self.health_device_fail:
            raise RuntimeError("bridge health device creation failed")
        if self.health_device_return_none:
            return None
        # Real Indigo ids are not 1, 2, 3 — offset so a test cannot mistake one
        # for an ordinary allow-listed device by id alone.
        dev = FakeHealthDevice(9000 + len(self.health_devices))
        self.health_devices.append(dev)
        self.devices.add(dev)  # so a re-resolve via device_getter finds it too
        return dev

    def _client(self, logger, prefs, **kwargs):
        client = FakeBridgeClient(logger, prefs, **kwargs)
        self.clients.append(client)
        return client

    @property
    def client(self) -> FakeBridgeClient:
        assert self.clients, "no bridge client was created"
        return self.clients[-1]

    def start(self, seed: bool = True) -> FakeBridgeClient:
        """Start the client and, by default, simulate its attach.

        The real client's handshake calls the endpoint provider before anything
        else, and building a spec is what seeds the per-device "last pushed"
        snapshot every later diff is measured against (E5). A harness that
        started the client without that would leave every ``device_updated``
        test diffing against nothing — a state the running plugin cannot be in,
        because a push is gated on being attached and an attach always ran the
        provider first.
        """
        self.bridge.start()
        if seed:
            self.bridge.endpoint_specs()
        return self.client


def per_device_warnings(logger) -> int:
    """How many times the per-DEVICE skip line was printed.

    Narrower than ``logger.warning.call_count`` on purpose: an allow-list that
    skips entirely also draws the one-per-cause "NONE of them can be bridged"
    summary (issue #141 follow-up), and the latch these tests pin is the
    per-device one.
    """
    return sum(1 for call in logger.warning.call_args_list
               if "is in the export list but will NOT be bridged" in str(call.args[0]))


def warnings_of(logger) -> str:
    return " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                    else str(call.args[0])
                    for call in logger.warning.call_args_list)


def errors_of(logger) -> str:
    return " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                    else str(call.args[0])
                    for call in logger.error.call_args_list)


def infos_of(logger) -> str:
    return " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                    else str(call.args[0])
                    for call in logger.info.call_args_list)


@pytest.fixture
def unbridgeable_role(bridge_mod, monkeypatch):
    """Make ``doorLock`` a role this build has no handler for, and return it.

    E4 completed the §4.2 table, so there is no longer a real role without a
    handler — but the skip path is not dead code. It is what a plugin does with
    an allow-list entry written by a *newer* version of itself (the export blob
    lives in plugin prefs and survives a downgrade), and with any role a future
    protocol version adds. Removing a real handler is the only honest way to
    reach it.
    """
    import export_handlers
    monkeypatch.delitem(export_handlers.HANDLERS, "doorLock")
    return "doorLock"


@pytest.fixture
def devices():
    return FakeIndigoDevices([
        RelayDevice(101, "Study Plug", onState=False),
        DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40),
        SprinklerDevice(104, "Irrigation"),
        RelayDevice(105, "Matter Plug", plugin_id=OURS),
    ])


# ---------------------------------------------------------------------------
# The attach-timeout formula
# ---------------------------------------------------------------------------
class TestAttachTimeout:
    """E3a's pacing×count interaction, answered without a protocol change."""

    def test_small_sets_keep_the_flat_floor(self):
        """The floor is over the count SENT — which is not always the count paced.

        ``attach_timeout_for(0)`` returning the floor is correct arithmetic and
        wrong as a deadline for the ONE caller that sends zero endpoints on
        purpose: the §3.1 ``replace_all`` un-export sends nothing and makes the
        node remove everything. That path must size its own deadline over the
        removals — see
        ``TestLifecycle.test_the_un_export_deadline_is_sized_by_the_removals``.
        """
        assert bridge_client.attach_timeout_for(0) == bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(20) == bridge_client.ATTACH_TIMEOUT

    def test_a_large_set_gets_more_time_than_its_pacing_costs(self):
        # ~100ms per removal (§3.3) means 80 endpoints can spend 8s in pacing
        # alone — precisely the flat deadline it would otherwise be given.
        assert bridge_client.attach_timeout_for(80) > 80 * 0.1
        assert bridge_client.attach_timeout_for(80) == pytest.approx(14.0)

    def test_the_crossover_off_the_floor_is_where_the_arithmetic_says(self):
        """T3: 2.0 + 0.15n passes 8.0 between 40 and 41, and nowhere else.

        Pinned because the crossover is the only observable consequence of the
        two constants — tune either and this is the test that notices.
        """
        assert bridge_client.attach_timeout_for(40) == bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(41) > bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(41) == pytest.approx(8.15)

    def test_it_is_monotonic_and_never_negative(self):
        values = [bridge_client.attach_timeout_for(n) for n in range(0, 200, 10)]
        assert values == sorted(values)
        assert bridge_client.attach_timeout_for(-5) == bridge_client.ATTACH_TIMEOUT


# ---------------------------------------------------------------------------
# The endpoint provider (§3.1 reconcile source)
# ---------------------------------------------------------------------------
class TestEndpointProvider:
    def test_builds_a_spec_from_the_live_device(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(102, "dimmableLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.indigo_device_id == 102
        assert spec.role == "dimmableLight"
        assert spec.label == "Hall Dimmer"
        assert spec.reachable is True
        assert spec.states == {"onOff": True, "level": 40}

    def test_a_spec_defaults_to_the_derived_published_identity(
            self, bridge_mod, mock_logger, devices):
        """Issues #219/#240 — an entry that has never moved off the default
        derivation still carries it in the spec, even though `to_wire()`
        omits it (the golden-frame round trip stays byte-identical)."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.published_as == "indigo-102"

    def test_a_spec_carries_the_stored_published_identity(self, bridge_mod, mock_logger, devices):
        """A re-adopted or role-changed entry's identity rides along verbatim."""
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(102, "dimmableLight", published_as="indigo-102~2")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.published_as == "indigo-102~2"

    def test_the_name_override_wins_over_the_indigo_name(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, "onOffLight", name_override="Desk Lamp")])
        assert h.bridge.endpoint_specs()[0].label == "Desk Lamp"

    def test_options_ride_along(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(102, "dimmableLight", options={"someKey": 1}))
        assert h.bridge.endpoint_specs()[0].options == {"someKey": 1}

    # -- issue #293: the effective CT-bounds wire transform ------------
    def test_an_ordinary_ct_export_has_no_bounds_on_the_wire(self, bridge_mod, mock_logger,
                                                             devices):
        """No seed, nothing learned — the wire options must stay `{}`, byte-
        identical to an export with no colour-temperature feature at all."""
        dev = DimmerDevice(800, "CT Lamp", onState=True, brightness=50, whiteTemperature=2700,
                            supportsWhiteTemperature=True)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        assert h.bridge.endpoint_specs()[0].options == {}

    def test_a_seeded_range_reaches_the_wire_as_the_effective_pair(
            self, bridge_mod, mock_logger, devices):
        dev = DimmerDevice(800, "CT Lamp", onState=True, brightness=50, whiteTemperature=2700,
                            supportsWhiteTemperature=True)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [
            ExportEntry(800, "colorTemperatureLight",
                       options={"ctMinMireds": 200, "ctMaxMireds": 400})])
        assert h.bridge.endpoint_specs()[0].options == {"ctMinMireds": 200, "ctMaxMireds": 400}

    def test_learned_keys_never_reach_the_wire_only_the_effective_pair_does(
            self, bridge_mod, mock_logger, devices):
        dev = DimmerDevice(800, "CT Lamp", onState=True, brightness=50, whiteTemperature=2700,
                            supportsWhiteTemperature=True)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [
            ExportEntry(800, "colorTemperatureLight",
                       options={"ctMinMireds": 200, "ctMaxMireds": 400,
                                "ctLearnedMaxMireds": 390})])
        options = h.bridge.endpoint_specs()[0].options
        assert options == {"ctMinMireds": 200, "ctMaxMireds": 390}
        assert "ctLearnedMaxMireds" not in options
        assert "ctLearnedMinMireds" not in options

    def test_a_seed_equal_to_generic_still_reaches_the_wire(
            self, bridge_mod, mock_logger, devices):
        """The un-trapdoor rule (issue #293's 2026-08-24 revision): a stored
        seed reaches the wire even when it resolves to exactly the generic
        153/500 pair — the gate is key PRESENCE, not "differs from
        generic". See ``ct_bounds.wire_options`` for the re-widen case
        (a learned bound settling back to generic) this exists to fix."""
        dev = DimmerDevice(800, "CT Lamp", onState=True, brightness=50, whiteTemperature=2700,
                            supportsWhiteTemperature=True)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [
            ExportEntry(800, "colorTemperatureLight",
                       options={"ctMinMireds": 153, "ctMaxMireds": 500})])
        assert h.bridge.endpoint_specs()[0].options == {"ctMinMireds": 153, "ctMaxMireds": 500}

    def test_ct_bounds_never_leak_into_an_unrelated_roles_wire_options(
            self, bridge_mod, mock_logger, devices):
        """Options for a role with no colour temperature carry only what that
        role's own keys mean — an invert flag rides untouched, and no CT
        keys are invented from nothing."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(102, "windowCovering", options={"invert": True}))
        assert h.bridge.endpoint_specs()[0].options == {"invert": True}

    def test_a_disabled_device_is_unreachable_not_absent(self, bridge_mod, mock_logger, devices):
        """XAC8: greyed out in the ecosystem beats timing out."""
        devices[101].enabled = False
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        assert h.bridge.endpoint_specs()[0].reachable is False

    def test_an_unconfigured_device_is_unreachable(self, bridge_mod, mock_logger, devices):
        devices[101].configured = False
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        assert h.bridge.endpoint_specs()[0].reachable is False

    def test_a_deleted_device_is_skipped_with_a_warning(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(999, "onOffLight")])
        assert h.bridge.endpoint_specs() == []
        assert "no longer exists" in warnings_of(mock_logger)

    def test_a_now_excluded_device_is_skipped_with_its_reason(self, bridge_mod, mock_logger,
                                                              devices):
        """The store is NOT the guard — classify is, on every attach."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        # A sprinkler cannot be exported at all, so it can only get in by a
        # restored backup or a hand-edited pref — exactly what this covers.
        h.store.upsert(ExportEntry(104, "onOffLight"))
        assert h.bridge.endpoint_specs() == []
        assert export_catalog.REASON_SPRINKLER in warnings_of(mock_logger)

    def test_our_own_device_never_reaches_the_node(self, bridge_mod, mock_logger, devices):
        """XAC6/XNG3 — the loop guard, re-run at endpoint-build time."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(105, "onOffLight"))
        assert h.bridge.endpoint_specs() == []
        assert export_catalog.REASON_LOOP_GUARD in warnings_of(mock_logger)

    def test_a_role_the_device_no_longer_offers_is_skipped(self, bridge_mod, mock_logger,
                                                           devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        # A plain relay never offers dimmableLight; the user's dimmer was
        # replaced by a relay under the same device id.
        h.store.upsert(ExportEntry(101, "dimmableLight"))
        assert h.bridge.endpoint_specs() == []
        assert "no longer offers" in warnings_of(mock_logger)

    def test_an_unbridgeable_role_is_skipped_rather_than_failing_the_whole_attach(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        """E3a: an unknown role fails the ENTIRE attach with ``internal``."""
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, unbridgeable_role), ExportEntry(102, "dimmableLight")])
        specs = h.bridge.endpoint_specs()
        assert [s.indigo_device_id for s in specs] == [102], "the good export must survive"
        assert "not one this plugin version can bridge" in warnings_of(mock_logger)

    def test_the_same_skip_is_warned_once_not_once_per_reconnect(self, bridge_mod, mock_logger,
                                                                 devices, unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        for _ in range(5):
            h.bridge.endpoint_specs()
        assert per_device_warnings(mock_logger) == 1

    def test_a_wholly_unbridgeable_list_says_so_once_and_names_the_devices(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        """⊗ Since #141 this state removes every accessory from every ecosystem.

        The node now restores its last-known endpoint set before going online,
        so an empty desired set against a non-empty allow-list is no longer the
        no-op it used to be — it is a full un-export (see
        ``bridge_client._replace_all``, which carries the §3.1 intent so the node
        does not refuse and halt the client instead). The user must be told the
        whole sentence, with the devices named, because the per-device lines are
        latched and will not re-print on the reconnect that does the removing.
        """
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, unbridgeable_role), ExportEntry(999, "onOffLight")])

        for _ in range(4):
            assert h.bridge.endpoint_specs() == []

        summaries = [call for call in mock_logger.warning.call_args_list
                     if "NONE of the" in str(call.args[0])]
        assert len(summaries) == 1, "once per cause, not once per reconnect"
        said = str(summaries[0].args[0]) % summaries[0].args[1:]
        assert "NONE of the 2 device(s)" in said
        assert "101:" in said and "999:" in said, "the devices are named, not merely counted"
        assert "endpoint numbers are kept" in said, "and the state is recoverable"

    def test_the_summary_is_silent_while_anything_at_all_is_bridgeable(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        """One survivor means the set is not empty, so no accessory is removed."""
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, unbridgeable_role), ExportEntry(102, "dimmableLight")])
        assert len(h.bridge.endpoint_specs()) == 1
        assert "NONE of the" not in warnings_of(mock_logger)

    def test_the_summary_latch_clears_so_a_recurrence_is_news_again(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        h.bridge.endpoint_specs()
        h.store.upsert(ExportEntry(101, "onOffLight"))       # fixed
        assert len(h.bridge.endpoint_specs()) == 1
        h.store.upsert(ExportEntry(101, unbridgeable_role))  # broken again
        h.bridge.endpoint_specs()

        summaries = [c for c in mock_logger.warning.call_args_list if "NONE of the" in str(c.args[0])]
        assert len(summaries) == 2

    def test_an_empty_allow_list_draws_no_summary_at_all(self, bridge_mod, mock_logger, devices):
        """Nothing declared is not "nothing bridgeable" — XG5 means no client."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        assert h.bridge.endpoint_specs() == []
        assert "NONE of the" not in warnings_of(mock_logger)

    def test_the_declared_count_is_the_store_not_the_classifier(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        """⊗ The one thing that tells the two empty attaches apart.

        `_declared_export_count` must report what the USER asked for, not what
        survived classification — the client compares the two, and defining it
        as ``len(endpoint_specs())`` would make them equal by construction and
        put the halt straight back.
        """
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, unbridgeable_role), ExportEntry(999, "onOffLight")])
        assert h.bridge.endpoint_specs() == []
        assert h.bridge._declared_export_count() == 2

    def test_a_changed_skip_reason_is_warned_again(self, bridge_mod, mock_logger, devices,
                                                   unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        h.bridge.endpoint_specs()
        h.store.upsert(ExportEntry(101, "dimmableLight"))   # now a different failure
        h.bridge.endpoint_specs()
        assert per_device_warnings(mock_logger) == 2


class TestEndpointProviderBattery:
    """Issue #220 — `_spec_for`'s `battery`/`batteryLevel` composition."""

    def test_a_battery_device_carries_battery_true_and_the_state_key(
            self, bridge_mod, mock_logger, devices):
        devices.add(RelayDevice(200, "Battery Plug", onState=True, batteryLevel=80))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(200, "onOffLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.battery is True
        assert spec.states["batteryLevel"] == 80

    def test_a_mains_device_carries_neither(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.battery is False
        assert "batteryLevel" not in spec.states

    def test_battery_level_zero_still_declares_a_battery_but_publishes_no_reading(
            self, bridge_mod, mock_logger, devices):
        """The asymmetry, pinned: `battery` is `is not None` (0 counts as HAS a
        battery attribute); the *value* 0 is suppressed as untrustworthy
        (issue #190) — the same attribute, two different tests, on purpose.
        """
        devices.add(RelayDevice(201, "Fresh Plug", onState=True, batteryLevel=0))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(201, "onOffLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.battery is True
        assert "batteryLevel" not in spec.states

    def test_battery_level_negative_one_still_declares_a_battery_but_publishes_no_reading(
            self, bridge_mod, mock_logger, devices):
        """Same evidence rule, a different untrustworthy value: `-1` is a real
        "unknown" sentinel some drivers use, so `is not None` still declares a
        battery (the device factually has the property) while `battery_percent`
        suppresses the reading itself. §4.2's 1-100 domain must never see it.
        """
        devices.add(RelayDevice(202, "Sentinel Plug", onState=True, batteryLevel=-1))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(202, "onOffLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.battery is True
        assert "batteryLevel" not in spec.states


# ---------------------------------------------------------------------------
# Client lifecycle (XG5)
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_an_empty_allow_list_starts_nothing(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge.exports_changed()
        assert h.bridge.active is False
        assert h.clients == []

    def test_the_first_export_starts_the_client(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.bridge.active is True
        assert h.client.ran is True

    def test_starting_twice_is_a_no_op(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.start()
        h.bridge.start()
        assert len(h.clients) == 1

    def test_emptying_the_allow_list_un_exports_deliberately_then_stops(
            self, bridge_mod, mock_logger, devices):
        """PRD §7: endpoints go, pairings stay — and it needs the §3.1 opt-in."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert client.only("attach") == ("attach", [], True), "must carry replace_all"
        assert client.closed is True
        assert h.bridge.active is False

    def test_the_un_export_deadline_is_sized_by_the_removals_not_the_empty_send(
            self, bridge_mod, mock_logger, devices):
        """X1: attach latency is dominated by REMOVALS, and this path removes all.

        The endpoint list sent is ``[]``, so letting the client derive the
        deadline from it hands the un-export of a 60-device database the flat 8s
        floor — while the node paces those 60 removals ~100ms apart (§3.3). It
        times out, warns that accessories "may linger", and then ``close()``
        yanks the socket out from under a reconcile that was going fine.
        """
        entries = [ExportEntry(200 + n, "onOffLight") for n in range(60)]
        h = Harness(bridge_mod, mock_logger, devices, entries)
        client = h.start()
        for entry in entries:
            h.store.remove(entry.indigo_device_id)
        h.bridge.exports_changed()

        assert client.attach_timeouts == [bridge_client.attach_timeout_for(60)]
        assert client.attach_timeouts[0] > 60 * 0.1, "must outlast the node's pacing"
        assert client.attach_timeouts[0] > bridge_client.ATTACH_TIMEOUT
        assert "may linger" not in warnings_of(mock_logger)

    def test_a_small_un_export_still_gets_the_floor(self, bridge_mod, mock_logger, devices):
        """Sizing by removals must not make the ordinary case slower to fail."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert client.attach_timeouts == [bridge_client.ATTACH_TIMEOUT]

    def test_the_client_is_dropped_even_if_the_final_attach_fails(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        client.fail["attach"] = ConnectionError("node is gone")
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.bridge.active is False
        # The warning must name what actually resumes it. "It will retry on its
        # own" was false: nothing retries until the client next comes up.
        said = warnings_of(mock_logger)
        assert "LINGER" in said
        assert "no retry loop" in said
        # F4: the socket must still be released — the client is unreachable from
        # here on, so a skipped close() leaks it until the plugin reloads.
        assert client.closed is True

    def test_stop_is_idempotent(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.bridge.stop()
        h.bridge.stop()
        assert h.bridge.active is False

    def test_the_client_gets_the_ws_port_pref(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.prefs[bridge_protocol.PREF_WS_PORT] = "5999"
        assert h.start().prefs[bridge_protocol.PREF_WS_PORT] == "5999"


# ---------------------------------------------------------------------------
# The E7 post-install poke (issue #135)
# ---------------------------------------------------------------------------
class TestRetryNow:
    def test_retry_now_delegates_to_the_client_when_present(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        assert h.bridge.retry_now() is True, "an accepting client's poke must be reported as landed"
        assert client.only("retry_now") == ("retry_now",)

    def test_retry_now_is_a_no_op_while_nothing_is_exported(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        assert h.bridge.retry_now() is False, "no client (XG5) means the poke never landed"
        assert h.clients == []

    def test_retry_now_reports_false_when_the_client_declines(self, bridge_mod, mock_logger, devices):
        # The TOCTOU race this guards against (self.client nulled by stop() on
        # another thread between the None-check and the call) is not
        # deterministically reproducible without patching a property onto
        # ExportBridge — this instead pins the local-capture code path's other
        # observable half: whatever the captured client's own retry_now()
        # answers is threaded straight through, exactly as it would be for a
        # client that declined because it raced a stop() to False.
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        client.retry_now = lambda: False
        assert h.bridge.retry_now() is False


# ---------------------------------------------------------------------------
# #154: reviving a client halted on version skew, after a bridge reinstall
# ---------------------------------------------------------------------------
class TestReviveAfterInstall:
    def test_is_a_no_op_while_nothing_is_exported(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        assert h.bridge.revive_after_install() is False, "no client (XG5) means nothing to revive"
        assert h.clients == []

    def test_is_a_no_op_when_the_client_is_not_halted(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        assert h.bridge.revive_after_install() is False
        assert h.bridge.client is client, "a live client must not be discarded"

    def test_is_a_no_op_when_halted_for_a_reason_a_reinstall_cannot_fix(
            self, bridge_mod, mock_logger, devices):
        """⊗ The critical gate. ``mass_removal_refused`` also HALTS the client
        (``bridge_client.HALTING_ATTACH_ERRORS``), but its remedy is the
        allow-list, not the node process — reviving it here would silently
        rebuild a client that attaches straight back into the same refusal.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        client.halted = True
        client.halted_reason = bridge_protocol.ERR_MASS_REMOVAL_REFUSED
        assert h.bridge.revive_after_install() is False
        assert h.bridge.client is client, "the wrong-reason halt must be left alone"
        assert client.closed is False

    def test_revives_a_client_halted_on_the_handshake_version_skew(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        old = h.start()
        old.halted = True
        old.halted_reason = "version_skew"
        assert h.bridge.revive_after_install() is True
        assert old.closed is True, "the halted client's socket must still be released"
        assert len(h.clients) == 2, "a fresh client must have been built"
        new = h.clients[-1]
        assert new is not old
        assert new.ran is True, "the fresh client's run loop must have been started"
        assert h.bridge.client is new

    def test_a_revival_resets_the_halt_report_latches(self, bridge_mod, mock_logger, devices):
        """⊗ Review finding: the latches survived the swap, so a reinstall that
        did NOT fix the skew re-halted in silence — the watchdog's standing
        "nothing is being exported" line stayed suppressed by the OLD client's
        report. A new client is a new outage history."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        old = h.start()
        old.halted = True
        old.halted_reason = "version_skew"
        h.bridge._halted_reported = True          # the old halt was reported
        h.bridge._recovery_reported = True
        h.bridge._refusal_reported = "version_mismatch"
        assert h.bridge.revive_after_install() is True
        assert h.bridge._halted_reported is False, "a re-halt of the NEW client must be reportable"
        assert h.bridge._recovery_reported is False
        assert h.bridge._refusal_reported is None

    def test_a_successor_client_built_mid_revival_is_not_dropped(
            self, bridge_mod, mock_logger, devices):
        """⊗ The `if self.client is client` guard, pinned. An unconditional
        null survived the whole suite (review mutation). The race window is
        between the guard's read and the null, so the swap is triggered FROM
        the guard's own halted_reason read — the only seam that interleaves
        exactly there.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        old = h.start()
        old.halted = True
        successor = h.make_client() if hasattr(h, "make_client") else type(old)(mock_logger, {})
        bridge = h.bridge

        class _SwapsOnRead:
            """halted_reason that installs a successor the moment it is read.

            A DATA descriptor (has __set__), deliberately: FakeBridgeClient's
            __init__ writes an instance attribute, and only a data descriptor
            outranks the instance dict on reads.
            """
            fired = False

            def __get__(self, obj, objtype=None):
                # Swap ONCE, on the guard's read. The log line reads
                # halted_reason again after the null — re-installing the
                # successor there would heal the very mutation this test
                # exists to kill (unconditional null → start() builds a
                # third client → caught below).
                if not _SwapsOnRead.fired:
                    _SwapsOnRead.fired = True
                    bridge.client = successor
                return "version_skew"

            def __set__(self, obj, value):
                pass  # the fixture's __init__ write; the read above is the law

        type(old).halted_reason = _SwapsOnRead()
        try:
            bridge.revive_after_install()
        finally:
            del type(old).halted_reason
        assert bridge.client is successor, (
            "revival nulled a successor client it never checked — the guard "
            "must only drop the exact object it validated")

    def test_revives_a_client_halted_on_the_attach_time_version_mismatch(
            self, bridge_mod, mock_logger, devices):
        """The other path to the same fact (bridge_client.py:403):
        ``HALTING_ATTACH_ERRORS`` also carries ``ERR_VERSION_MISMATCH``, and
        that path's ``halted_reason`` is the raw wire code
        (``"version_mismatch"``), not the handshake's ``"version_skew"``.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        old = h.start()
        old.halted = True
        old.halted_reason = bridge_protocol.ERR_VERSION_MISMATCH
        assert h.bridge.revive_after_install() is True
        assert h.clients[-1] is not old

    def test_logs_the_replaced_connection_line(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        client.halted = True
        client.halted_reason = "version_skew"
        h.bridge.revive_after_install()
        said = " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                        for c in mock_logger.info.call_args_list)
        assert "reinstalled" in said
        assert "halted" in said


# ---------------------------------------------------------------------------
# Indigo → node
# ---------------------------------------------------------------------------
class TestDeviceUpdated:
    def _harness(self, bridge_mod, mock_logger, devices, entry):
        h = Harness(bridge_mod, mock_logger, devices, [entry])
        h.start()
        return h

    def test_a_state_change_becomes_a_set_state(self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        before = RelayDevice(101, "Study Plug", onState=False)
        after = RelayDevice(101, "Study Plug", onState=True)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_state") == ("set_state", 101, {"onOff": True})

    def test_a_level_change_becomes_a_set_state(self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(102, "dimmableLight"))
        before = DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40)
        after = DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_state") == ("set_state", 102, {"level": 90})

    def test_an_unchanged_device_sends_nothing(self, bridge_mod, mock_logger, devices):
        """"Unchanged" means "equal to what we last PUSHED", since E5.

        ``devices[101]`` is ``onState=False``, so that is what the attach put on
        the wire and what this diff is measured against.
        """
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        same = RelayDevice(101, "Study Plug", onState=False)
        h.bridge.device_updated(same, RelayDevice(101, "Study Plug", onState=False))
        assert h.client.names() == []

    def test_a_rename_re_sends_the_spec_so_the_label_follows(self, bridge_mod, mock_logger,
                                                             devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        devices[101].name = "Desk Plug"
        h.bridge.device_updated(RelayDevice(101, "Study Plug"), devices[101])
        _name, spec = h.client.only("upsert_endpoint")
        assert spec.label == "Desk Plug"

    def test_a_rename_is_ignored_when_the_user_pinned_a_name(self, bridge_mod, mock_logger,
                                                             devices):
        h = self._harness(bridge_mod, mock_logger, devices,
                          ExportEntry(101, "onOffLight", name_override="Desk Lamp"))
        devices[101].name = "Something Else"
        h.bridge.device_updated(RelayDevice(101, "Study Plug"), devices[101])
        assert "upsert_endpoint" not in h.client.names()

    def test_disabling_a_device_sets_reachable_false(self, bridge_mod, mock_logger, devices):
        """XAC8 groundwork — the split §3.5 command, not a cluster state."""
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        before = RelayDevice(101, "Study Plug", enabled=True)
        after = RelayDevice(101, "Study Plug", enabled=False)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_reachable") == ("set_reachable", 101, False)

    def test_re_enabling_a_device_sets_reachable_true(self, bridge_mod, mock_logger, devices):
        """T3: the other direction of XAC8 — a device that comes back must say so.

        A one-way ``set_reachable`` leaves the accessory greyed out in every
        ecosystem forever, which looks exactly like a dead device.
        """
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        before = RelayDevice(101, "Study Plug", enabled=False)
        after = RelayDevice(101, "Study Plug", enabled=True)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_reachable") == ("set_reachable", 101, True)

    def test_an_unbridgeable_role_update_is_skipped_silently(self, bridge_mod, mock_logger,
                                                             devices, unbridgeable_role):
        """T3: the provider already warned; repeating it per state change is noise."""
        h = self._harness(bridge_mod, mock_logger, devices,
                          ExportEntry(101, unbridgeable_role))
        mock_logger.reset_mock()
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert h.client.names() == []
        assert mock_logger.warning.call_count == 0

    def test_a_device_removed_between_the_guard_and_here_is_dropped(self, bridge_mod,
                                                                    mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        h.store.remove(101)
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert h.client.names() == []

    def test_pushes_are_never_awaited(self, bridge_mod, mock_logger, devices):
        """§3.4 — a state push must not make Indigo's thread wait on Matter."""
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        h.client.fail["set_state"] = ConnectionError("socket died mid-write")
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        # It failed, it did not raise into Indigo, and it was NOT silent.
        assert "set_state dev 101 failed" in warnings_of(mock_logger)

    def test_a_learner_failure_does_not_stop_the_real_state_push(self, bridge_mod, mock_logger,
                                                                  devices):
        """Issue #294 review — `_feed_ct_learner` runs BEFORE the diff/push in
        `device_updated`, so a `_ct_learner.observe` exception must never be
        able to take the device's real state push down with it (the
        degradation-path convention: speculative code cannot discard a
        result the reliable path already found)."""
        dev = DimmerDevice(800, "CT Lamp", onState=True, brightness=29, whiteLevel=29,
                            whiteTemperature=2700, supportsWhiteTemperature=True)
        devices.add(dev)
        h = self._harness(bridge_mod, mock_logger, devices,
                          ExportEntry(800, "colorTemperatureLight"))

        def _blows_up(*_args, **_kwargs):
            raise RuntimeError("learner blew up")

        h.bridge._ct_learner.observe = _blows_up
        before = DimmerDevice(800, "CT Lamp", onState=True, brightness=29, whiteLevel=29,
                              whiteTemperature=2700, supportsWhiteTemperature=True)
        # A change well past CT_TOLERANCE_MIREDS (30) — a change within it
        # would be suppressed by the diff's own tolerance regardless of the
        # learner, and this test needs the push to be genuinely due.
        after = DimmerDevice(800, "CT Lamp", onState=True, brightness=29, whiteLevel=29,
                             whiteTemperature=2000, supportsWhiteTemperature=True)
        h.bridge.device_updated(before, after)
        _name, dev_id, states = h.client.only("set_state")
        assert dev_id == 800
        assert "colorTempMireds" in states

    def test_nothing_is_sent_before_the_attach_completes(self, bridge_mod, mock_logger, devices):
        """An incremental frame sent un-attached is refused (§1.1) and pointless."""
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        h.client.attached = False
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        h.bridge.upsert(101)
        h.bridge.remove(101)
        assert h.client.names() == []


class TestBatteryDeviceUpdated:
    """Issue #220 — a battery GAIN recreates via `upsert`; a loss does nothing special."""

    def test_a_battery_gain_upserts_instead_of_pushing_state(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        before = RelayDevice(101, "Study Plug", onState=False)
        devices[101].batteryLevel = 80
        h.bridge.device_updated(before, devices[101])
        assert h.client.names() == ["upsert_endpoint"]
        _name, spec = h.client.only("upsert_endpoint")
        assert spec.battery is True
        assert spec.states["batteryLevel"] == 80

    def test_a_battery_gain_warns_before_the_identity_destroying_recreate(
            self, bridge_mod, mock_logger, devices):
        """The gain branch used to log NOTHING before recreating the
        accessory — the same accessory-identity churn a role change costs,
        with no trace of why in the log.
        """
        devices.add(RelayDevice(103, "Study Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(103, "onOffLight")])
        h.start()
        before = RelayDevice(103, "Study Plug", onState=False)
        devices[103].batteryLevel = 80
        h.bridge.device_updated(before, devices[103])
        warnings = warnings_of(mock_logger)
        assert "103" in warnings and "started reporting a battery" in warnings
        assert "may need re-assigning" in warnings

    def test_a_battery_loss_triggers_no_upsert__the_cluster_is_monotonic(
            self, bridge_mod, mock_logger, devices):
        devices.add(RelayDevice(300, "Batt Plug", onState=True, batteryLevel=50))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(300, "onOffLight")])
        h.start()
        before = RelayDevice(300, "Batt Plug", onState=True, batteryLevel=50)
        devices[300].batteryLevel = None
        h.bridge.device_updated(before, devices[300])
        # No cluster removal exists to ask for, and the vanished reading alone
        # is reported (if at all) through the ordinary stopped-key path, not a
        # wire frame.
        assert h.client.names() == []

    def test_a_device_that_had_a_battery_all_along_is_unaffected(self, bridge_mod, mock_logger, devices):
        devices.add(RelayDevice(301, "Batt Plug", onState=True, batteryLevel=50))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(301, "onOffLight")])
        h.start()
        before = RelayDevice(301, "Batt Plug", onState=True, batteryLevel=50)
        devices[301].batteryLevel = 49
        h.bridge.device_updated(before, devices[301])
        assert h.client.only("set_state") == ("set_state", 301, {"batteryLevel": 49})

    def test_a_battery_gain_and_a_rename_in_the_same_update_is_one_upsert_carrying_the_new_label(
            self, bridge_mod, mock_logger, devices):
        """The gain branch returns early, before the ordinary rename check ever
        runs — so a rename landing in the SAME update must not be dropped.
        `upsert`'s own spec build (`_spec_for`) re-reads the device's current
        name, so one upsert already carries it; no separate push is needed.
        """
        devices.add(RelayDevice(302, "Study Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(302, "onOffLight")])
        h.start()
        before = RelayDevice(302, "Study Plug", onState=False)
        devices[302].batteryLevel = 80
        devices[302].name = "Desk Plug"
        h.bridge.device_updated(before, devices[302])
        assert h.client.names() == ["upsert_endpoint"]
        _name, spec = h.client.only("upsert_endpoint")
        assert spec.battery is True
        assert spec.states["batteryLevel"] == 80
        assert spec.label == "Desk Plug"

    def test_a_drain_to_zero_stops_reporting_it_rather_than_upserting_or_pushing_a_zero(
            self, bridge_mod, mock_logger, devices):
        """The intended UX (issue #190's reasoning applied to battery): a live
        77 -> 0 transition is NOT a battery loss (`batteryLevel` is still
        `is not None`, so neither the gain nor any loss branch fires) and NOT
        a value to push (0 is suppressed as untrustworthy) — it is exactly the
        "device stopped answering a key" case. The ecosystem keeps showing
        77% rather than a false "flat" alarm from a just-commissioned-style 0,
        and the only trace is the stopped-key streak warning.
        """
        devices.add(RelayDevice(303, "Batt Plug", onState=True, batteryLevel=77))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(303, "onOffLight")])
        h.start()
        before = RelayDevice(303, "Batt Plug", onState=True, batteryLevel=77)
        devices[303].batteryLevel = 0
        h.bridge.device_updated(before, devices[303])
        assert h.client.names() == [], "no upsert, and no set_state carrying batteryLevel: 0"
        warnings = warnings_of(mock_logger)
        assert "303" in warnings and "batteryLevel" in warnings


class TestIncrementalCrud:
    def test_upsert_sends_the_current_spec(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.bridge.upsert(102)
        _name, spec = h.client.only("upsert_endpoint")
        assert spec.indigo_device_id == 102 and spec.role == "dimmableLight"

    def test_a_failed_upsert_names_the_consequence_not_just_the_failure(
            self, bridge_mod, mock_logger, devices):
        """`_log_future`'s generic "X failed" line does not say whether that
        heals itself — for `upsert_endpoint` it does not, until the next
        reconnect/attach, and that has to be in the log line itself.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.client.fail["upsert_endpoint"] = ConnectionError("socket died mid-write")
        h.bridge.upsert(102)
        assert "not exported until the next reconnect/attach" in warnings_of(mock_logger)

    def test_upsert_of_an_unbridgeable_export_sends_nothing(self, bridge_mod, mock_logger,
                                                            devices, unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        h.start()
        h.bridge.upsert(101)
        assert h.client.names() == []

    def test_remove_drops_the_endpoint(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.bridge.remove(101)
        assert h.client.only("remove_endpoint") == ("remove_endpoint", 101)

    def test_a_role_change_is_a_remove_then_an_add(self, bridge_mod, mock_logger, devices):
        """§4.1 rejects a role change in place — ecosystems cache the type."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.replace(101)
        assert h.client.names() == ["remove_endpoint", "upsert_endpoint"]
        assert h.client.calls[1][1].role == "onOffLight"

    def test_a_role_change_sends_the_new_generation(self, bridge_mod, mock_logger, devices):
        """Issue #240 — the identity carried by the stored entry, not the old
        one, is what `replace()`'s add half sends: the node creates a fresh
        accessory rather than reviving the retired one."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.store.upsert(ExportEntry(101, "onOffLight", published_as="indigo-101~2"))
        h.bridge.replace(101)
        assert h.client.names() == ["remove_endpoint", "upsert_endpoint"]
        assert h.client.calls[1][1].published_as == "indigo-101~2"

    def test_a_role_change_for_a_vanished_entry_only_removes(self, bridge_mod, mock_logger,
                                                             devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.store.remove(102)
        h.bridge.replace(102)
        assert h.client.names() == ["remove_endpoint"]

    def test_a_vanished_entry_says_the_old_accessory_is_already_gone(
            self, bridge_mod, mock_logger, devices):
        """The remove lands FIRST, so returning here in silence leaves the
        accessory gone from every paired ecosystem with nothing in the log
        that says so."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.store.remove(102)
        h.bridge.replace(102)
        said = warnings_of(mock_logger)
        assert "has been removed from every paired ecosystem" in said
        assert "no longer in the export list" in said

    def test_an_unbuildable_replacement_says_the_old_accessory_is_already_gone(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        """Same fault, the other bail-out: the entry is still there but no
        spec can be built for it (a role this build cannot bridge, a device
        that vanished)."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.store.upsert(ExportEntry(101, unbridgeable_role))
        h.bridge.replace(101)
        assert h.client.names() == ["remove_endpoint"]
        assert "replacement could NOT be built" in warnings_of(mock_logger)

    def test_a_never_scheduled_identity_change_warns_rather_than_whispering(
            self, bridge_mod, mock_logger, devices):
        """`_fire`'s `lost` gate: most dropped coroutines are re-delivered by
        the next attach, so they are debug. This one is not — the remove has
        no backstop that puts the accessory back on its own."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.store.upsert(ExportEntry(101, "onOffLight", published_as="indigo-101~2"))
        h.runtime.is_running = False
        h.bridge.replace(101)
        assert "removed and NOT re-added" in warnings_of(mock_logger)

    def test_a_failed_identity_change_names_the_half_finished_state(
            self, bridge_mod, mock_logger, devices):
        """`_log_future`'s generic line cannot say this one: `replace()`
        removes before it adds, so its failure is not "nothing happened"."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.store.upsert(ExportEntry(101, "onOffLight", published_as="indigo-101~2"))
        h.client.fail["upsert_endpoint"] = ConnectionError("socket died mid-write")
        h.bridge.replace(101)
        assert "OLD accessory may already have been removed" in warnings_of(mock_logger)


# ---------------------------------------------------------------------------
# The migrate nudge — a full mid-session attach (issue #246)
# ---------------------------------------------------------------------------
def _migrate_status(endpoint_count=1):
    return bridge_protocol.parse_status({
        "commissioned": True, "fabrics": [], "endpointCount": endpoint_count,
        "endpoints": [], "drift": [], "driftChecked": True,
    })


class TestReattach:
    """`ExportBridge.reattach` — the §3.1 attach `menuMigrateExport` nudges
    with once its store commit lands (#246 design §3.3)."""

    def test_submits_attach_with_the_current_specs_and_their_own_deadline(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.client.status = _migrate_status()

        told = h.bridge.reattach()

        assert told is True
        name, endpoints, replace_all = h.client.only("attach")
        assert name == "attach"
        assert [spec.indigo_device_id for spec in endpoints] == [102]
        assert replace_all is False
        assert h.client.attach_timeouts == [bridge_mod.attach_timeout_for(1)]

    def test_with_no_client_is_a_no_op_false(self, bridge_mod, mock_logger, devices):
        """XG5: nothing is exported, so there is no client to nudge at all."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        assert h.bridge.reattach() is False

    def test_a_successful_reattach_routes_the_status_through_on_attached(
            self, bridge_mod, mock_logger, devices):
        """Unlike `upsert`/`replace`, this is a full attach — so its result has
        to reach the SAME place a reconnect's does (fabrics, warnings, the
        outage latches), not just "the RPC did not raise"."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.client.status = _migrate_status(endpoint_count=3)

        told = h.bridge.reattach()

        assert told is True
        assert "bridge node attached" in " ".join(
            str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
            for c in mock_logger.info.call_args_list)
        assert h.bridge.fabrics == []

    def test_a_failed_reattach_reports_not_told_and_says_the_store_stands(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.client.fail["attach"] = ConnectionError("socket died mid-attach")

        told = h.bridge.reattach()

        assert told is False
        assert "WAS saved" in errors_of(mock_logger)
        assert "catches up at the next reconnect/attach" in errors_of(mock_logger)

    def test_a_refusal_on_a_healthy_socket_names_the_code_and_claims_no_self_heal(
            self, bridge_mod, mock_logger, devices):
        """Issue #246 review finding 2 — a ``BridgeProtocolError`` is a
        REFUSAL, not a dropped socket: nothing is reconnecting to retry this,
        so the message must not borrow the ``ConnectionError`` branch's
        self-heals-on-its-own claim."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.client.fail["attach"] = bridge_protocol.BridgeProtocolError(
            bridge_protocol.ERR_MALFORMED_ARGS, "duplicate publishedAs")

        told = h.bridge.reattach()

        assert told is False
        said = errors_of(mock_logger)
        assert "REFUSED by the bridge node" in said
        assert bridge_protocol.ERR_MALFORMED_ARGS in said
        assert "duplicate publishedAs" in said
        assert "UNAPPLIED" in said
        assert "catches up at the next reconnect/attach" not in said

    def test_a_timeout_names_the_deadline_not_an_empty_string(
            self, bridge_mod, mock_logger, devices):
        """Issue #246 review finding 2 — ``TimeoutError`` (which
        ``asyncio.TimeoutError`` is an alias of since Python 3.11) stringifies
        to ``""``, so ``str(exc)`` must never be the source of the message —
        the deadline is."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.client.fail["attach"] = TimeoutError()

        told = h.bridge.reattach()

        assert told is False
        said = errors_of(mock_logger)
        assert "TIMED OUT waiting" in said
        assert "may or may not have applied" in said
        assert "— ." not in said, "no empty str(exc) artifact"
        deadline = bridge_mod.attach_timeout_for(1) + bridge_mod.REATTACH_RESULT_HEADROOM
        assert f"waiting {deadline:.0f}s" in said

    def test_no_client_logs_a_warning_the_dialogs_see_the_log_promise_needs(
            self, bridge_mod, mock_logger, devices):
        """The dialog's error says "see Event Log" for a saved-but-not-told
        migrate — this is that line for the mid-dialog-disconnect race that
        leaves no client at all to nudge (issue #246 review finding 2)."""
        h = Harness(bridge_mod, mock_logger, devices, [])

        told = h.bridge.reattach()

        assert told is False
        assert "no bridge client" in warnings_of(mock_logger)

    def test_migrate_composition_attach_carries_the_retargeted_device_and_identity(
            self, bridge_mod, mock_logger, devices):
        """Composition check for the #246 migrate commit, with the REAL
        ``ExportBridge.reattach`` against a ``FakeBridgeClient`` rather than a
        mock: ``ExportStore.replace_all`` moves an entry's DEVICE while
        KEEPING its identity (``server_menu_mixin._migrate_commit``'s one
        atomic write), and the resulting attach frame's endpoint spec has to
        carry exactly that — the TARGET device id, publishing the MIGRATED
        identity — with nothing in between building or mocking the spec.
        """
        devices.add(RelayDevice(106, "Garage Plug"))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.client.status = _migrate_status()
        # The migrate commit's one atomic write: source (101) dropped, target
        # (106) inherits its identity.
        h.store.replace_all([ExportEntry(106, "onOffPlugInUnit", published_as="indigo-101")])

        told = h.bridge.reattach()

        assert told is True
        _name, endpoints, replace_all = h.client.only("attach")
        assert replace_all is False
        assert len(endpoints) == 1
        (spec,) = endpoints
        assert spec.indigo_device_id == 106
        assert spec.published_as == "indigo-101"


# ---------------------------------------------------------------------------
# Node → Indigo (§5 command events)
# ---------------------------------------------------------------------------
class TestOnCommand:
    def _deliver(self, h, frame_name):
        data = FRAMES[frame_name]["data"]
        h.bridge.on_command(bridge_protocol.parse_command(data))

    def test_an_on_off_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                  mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "onOffLight")])
        self._deliver(h, "command_on_off")
        mock_indigo_base.device.turnOn.assert_called_once_with(devices[123456789])

    def test_a_set_level_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                    mock_indigo_base):
        devices.add(DimmerDevice(123456789, "Golden Lamp"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "dimmableLight")])
        self._deliver(h, "command_set_level")
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(
            devices[123456789], value=60)

    def test_a_set_color_temp_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                         mock_indigo_base):
        devices.add(DimmerDevice(900004, "Golden CT", whiteLevel=70,
                                 supportsWhiteTemperature=True))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(900004, "colorTemperatureLight")])
        self._deliver(h, "command_set_color_temp")
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteTemperature"] == 3125     # 1e6 / 320 mireds
        assert kwargs["whiteLevel"] == 70

    def test_a_set_color_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                    mock_indigo_base):
        devices.add(DimmerDevice(900005, "Golden RGB", supportsRGB=True))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(900005, "extendedColorLight")])
        self._deliver(h, "command_set_color")
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert set(kwargs) == {"redLevel", "greenLevel", "blueLevel"}
        assert kwargs["blueLevel"] == 100             # hue 210, saturation 80

    def test_a_command_for_an_unexported_device_is_refused_with_a_warning(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PRD §7 race row, against the golden frame for exactly this case."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        self._deliver(h, "command_unexported_device")
        assert "not exported" in warnings_of(mock_logger)
        mock_indigo_base.device.turnOff.assert_not_called()

    def test_a_command_for_a_vanished_device_is_refused(self, bridge_mod, mock_logger, devices,
                                                        mock_indigo_base):
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "onOffLight")])   # no such Indigo device
        self._deliver(h, "command_on_off")
        assert "no longer exists" in warnings_of(mock_logger)
        mock_indigo_base.device.turnOn.assert_not_called()

    def test_a_command_the_role_does_not_define_is_refused(self, bridge_mod, mock_logger,
                                                           devices, mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "onOffLight")])
        self._deliver(h, "command_set_level")            # a light that cannot dim
        assert "does not define" in warnings_of(mock_logger)
        mock_indigo_base.dimmer.setBrightness.assert_not_called()

    def test_a_command_for_an_unbridgeable_role_is_refused_not_attempted(
            self, bridge_mod, mock_logger, devices, mock_indigo_base, unbridgeable_role):
        """A command must never be attempted for a role this build cannot serve."""
        lock_id = FRAMES["command_lock"]["data"]["indigoDeviceId"]
        devices.add(RelayDevice(lock_id, "Front Door"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(lock_id, unbridgeable_role)])
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_lock"]["data"]))
        assert "cannot bridge" in warnings_of(mock_logger)
        mock_indigo_base.device.lock.assert_not_called()

    def test_a_lock_command_reaches_indigo_and_confirms_nothing(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PRD §7: dispatch the lock, then stop. No optimistic state write."""
        lock_id = FRAMES["command_lock"]["data"]["indigoDeviceId"]
        dev = RelayDevice(lock_id, "Front Door", onState=False,
                          ownerProps={"IsLockSubType": True})
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(lock_id, "doorLock")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_lock"]["data"]))
        mock_indigo_base.device.lock.assert_called_once_with(dev)
        # Nothing was pushed back: the ecosystem's `lockState` moves when the
        # real device does and `deviceUpdated` says so, not because we asked.
        assert h.client.names() == []

        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_unlock"]["data"]))
        mock_indigo_base.device.unlock.assert_called_once_with(dev)

    def test_a_failing_dispatch_is_logged_not_raised(self, bridge_mod, mock_logger, devices,
                                                     mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug"))
        mock_indigo_base.device.turnOn.side_effect = RuntimeError("server said no")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        self._deliver(h, "command_on_off")
        assert "server said no" in errors_of(mock_logger)


# ---------------------------------------------------------------------------
# Failure surfacing
# ---------------------------------------------------------------------------
class TestFailureSurfacing:
    def _bridge(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        return h

    def test_a_refused_attach_names_the_code(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_MASS_REMOVAL_REFUSED, "no intent")
        assert bridge_protocol.ERR_MASS_REMOVAL_REFUSED in errors_of(mock_logger)

    def test_an_invalid_endpoint_map_says_what_a_rebuild_does(self, bridge_mod, mock_logger,
                                                              devices):
        # #132: the old line promised the rebuild WILL duplicate accessories.
        # It cannot — it renumbers nothing.
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "unreadable")
        assert "renumbers nothing" in errors_of(mock_logger)

    def test_version_skew_names_what_the_plugin_needs(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_version_skew(bridge_protocol.Hello(2, "9.9.9", "1.0"))
        said = errors_of(mock_logger)
        assert "Install/update the Matter bridge" in said
        # The requirement, not a promise the menu cannot yet keep: the pinned
        # bridge-node version bumps in the release commit AFTER this PR.
        assert "needs the paired bridge-node release" in said
        assert "installs the exact node version" not in said

    def test_drift_is_reported_never_repaired(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_drift_detected(bridge_protocol.parse_drift(
            FRAMES["drift_detected"]["data"]["drift"]))
        assert "DRIFT" in errors_of(mock_logger)
        assert h.client.names() == [], "drift must not trigger a repair"

    def test_drift_message_says_reset_renumbering_is_adopted_automatically(
            self, bridge_mod, mock_logger, devices):
        # #140: a factory-reset renumbering no longer reaches this handler at
        # all (the node adopts it itself) — so drift that DOES arrive here is a
        # real anomaly, and the message must say so, not point at the reset.
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_drift_detected(bridge_protocol.parse_drift(
            FRAMES["drift_detected"]["data"]["drift"]))
        said = errors_of(mock_logger)
        # The version named is the BRIDGE NODE's, not the plugin's — adoption
        # lives node-side, and a new plugin driving an old node still gets
        # reset drift here (claiming otherwise would be #132 again).
        assert "0.8.0" in said
        assert "renumbering automatically" in said
        assert "outside any reset" in said.lower()

    def test_an_unreachable_node_is_reported_once_per_outage(self, bridge_mod, mock_logger,
                                                             devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        for attempt in range(1, 6):
            h.bridge._on_unreachable(attempt)
        assert mock_logger.warning.call_count == 1
        said = warnings_of(mock_logger)
        assert "not responding" in said
        # E7: Indigo control must never be implicated by an export-side fault.
        assert "Indigo devices and inbound Matter control are unaffected" in said


class TestHealthTick:
    def _bridge(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        return h

    def test_an_inactive_bridge_says_nothing(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge.health_tick()
        assert mock_logger.warning.call_count == 0

    def test_an_attached_client_says_nothing(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        for _ in range(10):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 0

    def test_a_disconnected_client_warns_once_after_about_a_minute(self, bridge_mod, mock_logger,
                                                                   devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        for _ in range(10):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 1
        assert "~1 min" in warnings_of(mock_logger)

    def test_a_halted_client_says_so_once_per_streak(self, bridge_mod, mock_logger, devices):
        """Halted is not transient: nothing is coming to fix it on its own.

        F9: which is exactly why it must not be said every 15s tick, forever —
        the state never changes, so the repeat carries no new information and
        buries everything else in the event log.
        """
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.halted = True
        h.client.halted_reason = "version_skew"
        for _ in range(20):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 1
        assert "HALTED" in warnings_of(mock_logger)
        assert "version_skew" in warnings_of(mock_logger)

    def test_a_recovered_client_can_warn_again(self, bridge_mod, mock_logger, devices):
        """Once per STREAK, not once per process: a second outage must be heard."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.halted = True
        h.bridge.health_tick()
        h.client.halted = False
        h.bridge._on_attached(bridge_protocol.StatusReport(
            commissioned=True, fabrics=[], endpoint_count=1, endpoints=[], drift=[]))
        h.client.halted = True
        h.bridge.health_tick()
        assert mock_logger.warning.call_count == 2

    def test_the_recovery_state_says_nothing_is_exported_once(self, bridge_mod, mock_logger,
                                                               devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.client.recovery = True
        for _ in range(20):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 1
        assert "rebuild" in warnings_of(mock_logger)


# ---------------------------------------------------------------------------
# Nothing that stops export may be silent (the PR #124 silent-failure sweep)
# ---------------------------------------------------------------------------
class TestSilentFailures:
    """Every path here used to `return` with no trace of what was dropped."""

    def _bridge(self, bridge_mod, mock_logger, devices, role="onOffLight"):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, role)])
        h.start()
        return h

    def test_a_drop_while_merely_unattached_names_the_device_at_debug(
            self, bridge_mod, mock_logger, devices):
        """F1: attach WILL reconcile this, so it is debug — but not silence."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.bridge.upsert(101)
        debug = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                         else str(call.args[0])
                         for call in mock_logger.debug.call_args_list)
        assert "101" in debug
        assert mock_logger.warning.call_count == 0

    @pytest.mark.parametrize("state,expected", [
        ("halted", "HALTED (version_skew)"),
        ("recovery", "endpoint-map"),
    ])
    def test_a_drop_while_halted_or_in_recovery_is_a_warning_with_the_reason(
            self, bridge_mod, mock_logger, devices, state, expected):
        """F1: the client's own loud path is unreachable from here — replicate it.

        ``_live_client`` gates before ``set_state`` is ever called, so
        ``BridgeClient._log_dropped_state_push``'s warning is dead code on this
        route: a halted bridge dropped every push in total silence.
        """
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        setattr(h.client, state, True)
        h.client.halted_reason = "version_skew"
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert expected in warnings_of(mock_logger)
        assert "101" in warnings_of(mock_logger)

    def test_a_key_a_device_STOPS_reporting_is_said_once(
            self, bridge_mod, mock_logger, devices):
        """The protocol has no way to push an absence, so the ecosystem keeps
        the last value: a flat sensor reads as its final temperature forever."""
        bridge = bridge_mod.ExportBridge.__new__(bridge_mod.ExportBridge)
        bridge._logger = mock_logger
        bridge._stopped_keys = {}
        dev = RelayDevice(1, "Sensor")
        for _ in range(3):
            bridge._report_stopped_keys(dev, "temperatureSensor", frozenset({"temperature"}))
        assert mock_logger.warning.call_count == 1
        assert "stopped reporting" in warnings_of(mock_logger)

    def test_a_SECOND_lost_key_is_news_even_after_the_first(
            self, bridge_mod, mock_logger, devices):
        """⊗ The latch is on the key SET, not on "there is a gap".

        Keyed on the device alone, a sensor that loses its temperature and then
        ALSO loses its humidity says nothing the second time — and the second
        loss is a different fault with a different cause. Nothing exercised the
        second loss, so the weaker latch survived.
        """
        bridge = bridge_mod.ExportBridge.__new__(bridge_mod.ExportBridge)
        bridge._logger = mock_logger
        bridge._stopped_keys = {}
        dev = RelayDevice(1, "Sensor")
        bridge._report_stopped_keys(dev, "temperatureSensor", frozenset({"temperature"}))
        bridge._report_stopped_keys(dev, "temperatureSensor", frozenset({"temperature"}))
        assert mock_logger.warning.call_count == 1
        bridge._report_stopped_keys(dev, "temperatureSensor",
                                   frozenset({"temperature", "humidity"}))
        assert mock_logger.warning.call_count == 2, "a NEW lost key is news again"

    def test_the_skip_latch_RE_ARMS_once_the_device_is_bridgeable_again(
            self, bridge_mod, mock_logger, devices):
        """⊗ Clearing the latch on a successful spec is what makes it a latch.

        Without it the reason is remembered for the life of the plugin, so a
        device that breaks, is fixed, and breaks again the same way is skipped
        in total silence the second time — and the user has no idea their
        export stopped working again.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        devices.drop(101)
        h.bridge.endpoint_specs()
        assert "no longer exists" in warnings_of(mock_logger)
        first = mock_logger.warning.call_count

        devices.add(RelayDevice(101, "Study Plug"))
        h.bridge.endpoint_specs()               # healthy again — latch must clear
        devices.drop(101)
        h.bridge.endpoint_specs()               # the SAME fault must be news again

        assert mock_logger.warning.call_count > first

    def test_the_halted_drop_warning_is_once_per_streak(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.client.halted = True
        for _ in range(10):
            h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                    RelayDevice(101, "P", onState=True))
        assert mock_logger.warning.call_count == 1

    def test_a_failed_run_loop_schedule_is_a_warning_not_a_debug_line(
            self, bridge_mod, mock_logger, devices):
        """F2: if run() never got scheduled, NOTHING is exported, ever."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.runtime.is_running = False
        h.bridge.start()
        assert "bridge client run loop" in warnings_of(mock_logger)

    def test_a_failed_un_export_schedule_is_a_warning(self, bridge_mod, mock_logger, devices):
        """F2: the accessories stay in every paired ecosystem, unexplained."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.runtime.is_running = False
        h.store.remove(101)
        h.bridge.exports_changed()
        assert "un-exporting everything" in warnings_of(mock_logger)

    def test_an_ordinary_push_that_cannot_be_scheduled_stays_at_debug(
            self, bridge_mod, mock_logger, devices):
        """F2: a set_state is re-delivered by the next attach; it is not a loss."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.runtime.is_running = False
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert mock_logger.warning.call_count == 0
        assert mock_logger.debug.called

    def test_a_failed_dispatch_corrects_the_ecosystem_back_to_the_truth(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """F5: otherwise Home shows the light the user asked for and Indigo does not.

        The ecosystem applied the command optimistically the moment it sent it.
        Logging and returning leaves those two beliefs permanently split until
        something else happens to that device.
        """
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        mock_indigo_base.device.turnOn.side_effect = RuntimeError("server said no")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        assert h.client.only("set_state") == ("set_state", 123456789, {"onOff": False})
        assert "still shows" in errors_of(mock_logger)

    def test_a_state_read_failure_dedupes_on_the_reason_not_the_message(
            self, bridge_mod, mock_logger, devices):
        """F8b: a varying ``str(exc)`` in the key defeats the dedupe entirely."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        calls = {"n": 0}

        def exploding_states_for(_dev, _options=None):
            calls["n"] += 1
            raise RuntimeError(f"transient read error #{calls['n']}")

        handler = bridge_mod.export_handlers.handler_for("onOffLight")
        original, handler.states_for = handler.states_for, exploding_states_for
        try:
            for _ in range(5):
                h.bridge.endpoint_specs()
        finally:
            handler.states_for = original
        assert per_device_warnings(mock_logger) == 1
        assert "transient read error #1" in warnings_of(mock_logger), \
            "the varying detail belongs in the LINE, just not in the key"

    def test_a_non_terminal_attach_refusal_is_reported_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        """F8c: a transient refusal reconnects on backoff — this fires every cycle."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        for _ in range(10):
            h.bridge._on_attach_refused(bridge_protocol.ERR_INTERNAL, "node is confused")
        assert mock_logger.error.call_count == 1

    def test_a_terminal_refusal_is_still_said_every_time(self, bridge_mod, mock_logger, devices):
        """Terminal refusals do not loop, and each one is a distinct decision."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "unreadable")
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "unreadable")
        assert mock_logger.error.call_count == 2

    def test_a_reattach_lets_a_refusal_be_reported_again(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_INTERNAL, "node is confused")
        h.bridge._on_attached(bridge_protocol.StatusReport(
            commissioned=True, fabrics=[], endpoint_count=1, endpoints=[], drift=[]))
        h.bridge._on_attach_refused(bridge_protocol.ERR_INTERNAL, "node is confused again")
        assert mock_logger.error.call_count == 2

    def test_a_failing_diff_names_the_device_and_repeats_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        """F6/F7: a bare traceback per state change tells you nothing and never stops."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        handler = bridge_mod.export_handlers.handler_for("onOffLight")
        original = handler.diff_from
        handler.diff_from = lambda _p, _n, _opt=None: (
            _ for _ in ()).throw(RuntimeError("bad device"))
        try:
            for _ in range(10):
                h.bridge.device_updated(RelayDevice(101, "Study Plug", onState=False),
                                        RelayDevice(101, "Study Plug", onState=True))
        finally:
            handler.diff_from = original
        assert mock_logger.exception.call_count == 1
        assert "Study Plug" in errors_of(mock_logger)
        assert "101" in errors_of(mock_logger)

    def test_a_failed_dispatch_corrects_with_the_exports_own_options(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PR #125 C1: the corrective push used to drop the export's §4.1 options.

        A covering whose motor is wired backwards carries ``invert``. Read
        WITHOUT it, ``states_for`` answers ``100 - actual`` — so a failed
        ``goToPosition`` on such a blind "corrected" the ecosystem to the mirror
        image of where the blind really is. That is worse than the stale value it
        replaced: it is a wrong answer pushed with the authority of a fresh
        reading, and nothing later contradicts it.

        Brightness 30 with ``invert`` is §4.2 position 70. The bug pushed 30.
        """
        dev = DimmerDevice(700, "Back Blind", onState=True, brightness=30)
        devices.add(dev)
        mock_indigo_base.dimmer.setBrightness.side_effect = RuntimeError("motor jammed")
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(700, "windowCovering", options={"invert": True})])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=700, command="goToPosition", args={"position": 90}))
        assert h.client.only("set_state") == ("set_state", 700, {"position": 70})

    def test_a_correction_with_nothing_readable_says_so_rather_than_returning(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PR #125 H1: the error line one above promises "pushing the real one back".

        A lock whose ``onState`` has gone ``None`` has no truth to push, so
        ``states_for`` is empty and the push never happened — after the log had
        already said it would. Silence there is the worst possible outcome for
        the one role where the user is standing at the door.
        """
        dev = RelayDevice(701, "Front Door", onState=None,
                          ownerProps={"IsLockSubType": True})
        devices.add(dev)
        mock_indigo_base.device.lock.side_effect = RuntimeError("mesh unreachable")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(701, "doorLock")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=701, command="lock", args={}))
        assert h.client.names() == []
        assert "cannot push truth" in warnings_of(mock_logger)

    def test_a_device_that_stops_reporting_a_key_is_named_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        """PR #125 C4: the ONLY trace of a published key vanishing.

        ``diff`` iterates the new snapshot, so a key that disappeared produced
        no push and no log — and a reconnect does not heal it either, because
        ``attach`` sends the same partial snapshot and §3.4 leaves absent keys
        untouched. The ecosystem shows the last value it was told, indefinitely.
        Keeping that value is the decision; being quiet about it was the bug.
        """
        devices.add(RelayDevice(702, "Front Door", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(702, "doorLock")])
        h.start()
        for _ in range(6):
            h.bridge.device_updated(RelayDevice(702, "Front Door", onState=True),
                                    RelayDevice(702, "Front Door", onState=None))
        assert mock_logger.warning.call_count == 1
        warnings = warnings_of(mock_logger)
        assert "702" in warnings and "locked" in warnings
        assert "last known value" in warnings
        assert h.client.names() == [], "an absent key must not be fabricated onto the wire"

    def test_a_key_that_starts_reporting_again_re_arms_the_latch(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(703, "doorLock")])
        devices.add(RelayDevice(703, "Side Door", onState=True))
        h.start()
        h.bridge.device_updated(RelayDevice(703, "Side Door", onState=True),
                                RelayDevice(703, "Side Door", onState=None))
        h.bridge.device_updated(RelayDevice(703, "Side Door", onState=None),
                                RelayDevice(703, "Side Door", onState=True))
        h.bridge.device_updated(RelayDevice(703, "Side Door", onState=True),
                                RelayDevice(703, "Side Door", onState=None))
        assert mock_logger.warning.call_count == 2

    def test_a_command_that_lawfully_did_nothing_is_named_once_per_streak(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PR #125 M1: ``stopMotion`` on a dimmer-modelled blind.

        Neither an error (§4.2 declares the command for this role) nor a
        success worth being silent about: the user pressed stop and the blind
        kept going. It used to be a debug line from a stateless handler that
        could not name the device.
        """
        devices.add(DimmerDevice(704, "Landing Blind", onState=True, brightness=50))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(704, "windowCovering")])
        h.start()
        for _ in range(4):
            h.bridge.on_command(bridge_protocol.BridgeCommand(
                indigo_device_id=704, command="stopMotion", args={}))
        assert mock_logger.warning.call_count == 1
        assert "704" in warnings_of(mock_logger)
        assert "changed nothing" in warnings_of(mock_logger)
        mock_indigo_base.dimmer.setBrightness.assert_not_called()

    def test_a_command_that_did_something_clears_the_no_op_latch(
            self, bridge_mod, mock_logger, devices):
        devices.add(DimmerDevice(705, "Landing Blind", onState=True, brightness=50))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(705, "windowCovering")])
        h.start()
        stop = bridge_protocol.BridgeCommand(indigo_device_id=705, command="stopMotion", args={})
        h.bridge.on_command(stop)
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=705, command="goToPosition", args={"position": 40}))
        h.bridge.on_command(stop)
        assert mock_logger.warning.call_count == 2


# ---------------------------------------------------------------------------
# E5 — the un-export that did not land (XAC7)
# ---------------------------------------------------------------------------
class TestPendingUnExport:
    """The orphaned-accessories-forever gap, closed.

    Emptying the allow-list is the ONE moment the plugin ever tells the node to
    remove everything (§3.1 ``intent: replace_all``). If that attach does not
    land — the node is down, launchd has not restarted it, the plugin is
    reloading — nothing ever says it again: the allow-list is empty, so XG5 says
    no client, so there is no connection, so there is no attach. Every exported
    accessory stays in every paired ecosystem for good, controlling nothing.
    """

    def _emptied_with_a_dead_node(self, bridge_mod, mock_logger, devices, count=1):
        entries = [ExportEntry(200 + n, "onOffLight") for n in range(count)]
        h = Harness(bridge_mod, mock_logger, devices, entries)
        client = h.start()
        client.fail["attach"] = ConnectionError("node is gone")
        for entry in entries:
            h.store.remove(entry.indigo_device_id)
        h.bridge.exports_changed()
        return h

    def test_a_failed_un_export_is_recorded_in_prefs(self, bridge_mod, mock_logger, devices):
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices, count=3)
        assert h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] == 3
        assert h.saves >= 1, "the debt has to reach disk to survive the reload it covers"

    def test_a_successful_un_export_leaves_no_debt(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs

    def test_the_debt_reconnects_even_though_nothing_is_exported(
            self, bridge_mod, mock_logger, devices):
        """XG5 says no client while nothing is exported. This is the exception."""
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        assert h.bridge.active is False

        h.bridge.exports_changed()          # e.g. the next startup, or a dialog close

        assert h.bridge.active is True, "an outstanding un-export must reconnect on its own"
        assert h.bridge._pending_replace_all() == 1

    def test_the_reconnected_attach_carries_the_replace_all_intent(
            self, bridge_mod, mock_logger, devices):
        """Without the intent the node answers `mass_removal_refused`, forever."""
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.bridge.exports_changed()
        provider = h.client.kwargs["replace_all_provider"]
        assert provider() == 1

    def test_the_provider_answers_a_COUNT_because_the_deadline_is_built_from_it(
            self, bridge_mod, mock_logger, devices):
        """⊗ A bool here is an outage, not a style point.

        `BridgeClient` declares `Callable[[], int]` and feeds the answer to
        `attach_timeout_for`, because the discharge attach sends nothing while
        removing everything — so `len(specs)` is exactly the wrong number. A
        wrapper returning `bool` sailed through the client's `int()` as 1, so
        every discharge got the 8s floor: an 80-accessory un-export timed out
        mid-reconcile, was torn down and retried, forever.
        """
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices, count=80)

        provider = h.client.kwargs["replace_all_provider"]
        assert provider() == 80, "the DEBT is 80 endpoints, not 'yes'"
        assert not isinstance(provider(), bool)
        # And the deadline the un-export actually asked for is sized over it.
        assert h.client.attach_timeouts[-1] == bridge_client.attach_timeout_for(80)
        assert h.client.attach_timeouts[-1] > bridge_client.ATTACH_TIMEOUT

    def test_a_later_un_export_never_SHRINKS_an_outstanding_debt(
            self, bridge_mod, mock_logger, devices):
        """⊗ The node is still holding the first un-export's accessories.

        Overwriting under-sized the deadline for the removals it actually owes:
        80 unpaid, then one device exported and removed again wrote 1, and the
        discharge attach that has to remove 81 records was given the 8s floor.
        """
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] = 80
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        # The node is still down, so this un-export does not land either — which
        # is the only state in which the recorded count is still observable.
        h.client.fail["attach"] = ConnectionError("node is still gone")
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.bridge._pending_replace_all() == 80
        # ...and the deadline the discharge will ask for covers all 80, not 1.
        assert h.client.attach_timeouts[-1] == bridge_client.attach_timeout_for(80)

    def test_an_empty_to_empty_transition_never_ERASES_an_outstanding_debt(
            self, bridge_mod, mock_logger, devices):
        """⊗ Recording 0 used to POP the pref, destroying an unpaid debt.

        `exports_changed` reconnects with no exports purely to discharge a
        debt, which sets `_last_export_count` to 0 — so the very next empty
        transition arrived at the recorder with `removing == 0` and wiped a
        debt of 5 that nothing had discharged. If the attach then failed, the
        only surviving record that five accessories should be gone was gone
        with it, and no later attach would ever carry the intent again.
        """
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] = 5
        h.bridge.exports_changed()          # the debt-driven reconnect (count 0)
        assert h.bridge.active is True
        h.client.fail["attach"] = RuntimeError("node went away mid-un-export")

        h.bridge.exports_changed()          # empty → empty, with a live client

        assert h.bridge._pending_replace_all() == 5, "an unpaid debt must survive"

    def test_an_unreadable_flag_SAYS_SO_rather_than_reading_as_nothing_owed(
            self, bridge_mod, mock_logger, devices):
        """The debt is the only thing that makes an un-export recoverable."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] = "not a number"
        assert h.bridge._pending_replace_all() == 0
        said = " ".join(str(call.args[0]) for call in mock_logger.warning.call_args_list)
        assert "unreadable" in said

    def test_a_re_populated_allow_list_does_NOT_cancel_the_debt(
            self, bridge_mod, mock_logger, devices):
        """⊗ The debt survives a disjoint re-add, or the client halts forever.

        The old rule ANDed the debt with an empty allow-list — "a re-populated
        list supersedes it" — and the two are not alternatives. Empty the list
        while the node is down (debt), then export a *different* device: the
        node still has to remove everything it holds, its §3.1 guard still sees
        zero survivors, and an attach without the intent is refused with
        `mass_removal_refused`, which HALTS the client permanently behind a
        message blaming the allow-list.
        """
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.bridge._pending_replace_all() == 1

    def test_a_successful_attach_discharges_the_debt_and_drops_the_client(
            self, bridge_mod, mock_logger, devices):
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.bridge.exports_changed()
        client = h.client

        h.bridge._on_attached(bridge_protocol.parse_status(
            FRAMES["attach_replace_all"]["response"]["result"]), True)

        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs
        assert client.closed is True, "XG5: nothing exported, so nothing needs a socket"
        assert h.bridge.active is False
        assert "outstanding un-export completed" in " ".join(
            str(call.args[0]) for call in mock_logger.info.call_args_list)

    def test_an_attach_that_did_NOT_carry_the_intent_discharges_nothing(
            self, bridge_mod, mock_logger, devices):
        """⊗ Discharge on what the attach SENT, never on live state.

        Reading "is a debt recorded now?" here wipes a debt written *while this
        attach was in flight*: the user empties the allow-list at the moment an
        ordinary reconnect lands, that attach carried no intent and removed
        nothing, and one step later the flag is cleared with a log line
        asserting an un-export that never happened. The accessories stay in
        every ecosystem and nothing anywhere knows they should not.
        """
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.bridge.exports_changed()

        h.bridge._on_attached(bridge_protocol.parse_status(
            FRAMES["attach_replace_all"]["response"]["result"]), False)

        assert h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] == 1
        assert "outstanding un-export completed" not in " ".join(
            str(call.args[0]) for call in mock_logger.info.call_args_list)

    def test_a_debt_discharged_beside_real_exports_keeps_the_client(
            self, bridge_mod, mock_logger, devices):
        """XG5 hangs up only when nothing is exported — not merely on discharge."""
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        client = h.client

        h.bridge._on_attached(bridge_protocol.parse_status(
            FRAMES["attach_replace_all"]["response"]["result"]), True)

        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs
        assert client.closed is False, "there is something exported; it needs the socket"
        assert h.bridge.active is True

    def test_an_unreadable_flag_is_treated_as_no_debt(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] = "not a number"
        assert h.bridge._pending_replace_all() == 0
        h.bridge.exports_changed()
        assert h.bridge.active is False


class TestStartGating:
    """The destroy-then-recreate race around the un-export."""

    def test_start_during_an_un_export_is_deferred_not_dropped(
            self, bridge_mod, mock_logger, devices, monkeypatch):
        """A user who empties the list and immediately re-adds must end up connected.

        ``_replace_all_then_stop`` clears ``self.client`` the instant it fires,
        so an unguarded ``start()`` would build a second client on top of the
        socket the first is still using to say "remove everything" — and then
        race it to the same node, which supersedes one of them.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        first = h.start()
        started_during: list = []

        async def _slow_attach(endpoints=None, *, replace_all=False, timeout=None):
            # Stand in for a node that takes its time over the paced removals.
            h.store.upsert(ExportEntry(101, "onOffLight"))
            h.bridge.exports_changed()
            started_during.append(len(h.clients))
            return None

        monkeypatch.setattr(first, "attach", _slow_attach)
        h.store.remove(101)
        h.bridge.exports_changed()

        assert started_during == [1], "no second client while the un-export is in flight"
        assert len(h.clients) == 2, "and the deferred start still happened afterwards"
        assert h.bridge.active is True

    def test_a_deferred_start_is_honoured_even_when_the_un_export_never_RAN(
            self, bridge_mod, mock_logger, devices):
        """⊗ The fire-failure path releases the gate AND the deferred start.

        `_un_exporting` gates `start()`, and only the coroutine's `finally`
        clears it — so if the coroutine was never scheduled at all, a user who
        empties the allow-list and immediately re-adds a device gets a
        permanently inert bridge on top of an un-export that also did not
        happen. Only the flag was pinned, not the start it was holding back.
        """
        devices.add(RelayDevice(101, "Lamp", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()

        # `save_prefs` is called from INSIDE the un-export window — it is the
        # plugin's own `savePluginPrefs`, and the debt is written before the
        # coroutine is fired. Re-adding a device from there is the shortest
        # honest route to the state the recovery block exists for: a start
        # request parked behind an un-export that then never ran at all.
        def _save_and_re_add():
            if h.bridge._un_exporting and not h.bridge._start_after_un_export:
                h.runtime.is_running = False    # ...and the loop dies with it
                h.bridge.start()                # deferred, not performed

        h.bridge._save_prefs = _save_and_re_add

        h.store.remove(101)
        h.bridge.exports_changed()          # the un-export cannot be fired

        assert h.bridge._un_exporting is False, "the gate must not stick shut"
        assert h.bridge.active is True, (
            "the start parked behind the un-export must still happen — the "
            "coroutine whose `finally` would have done it never ran")


# ---------------------------------------------------------------------------
# E5 — diffing against what was pushed, not against the last Indigo reading
# ---------------------------------------------------------------------------
class TestPushedSnapshot:
    def _colour(self, devices, green):
        return DimmerDevice(300, "Strip", supportsColor=True, supportsRGB=True,
                            onState=True, brightness=100,
                            redLevel=100, greenLevel=green, blueLevel=0)

    def test_a_sub_tolerance_ramp_still_reaches_the_ecosystem(
            self, bridge_mod, mock_logger, devices):
        """The bug this whole change exists for: unbounded, silent drift.

        Hue carries a ±1° tolerance (Matter's 0-254 hue round-trips ±1°). One
        unit of green against full red moves the recovered hue about 0.6°, so
        every single step of a ramp compares "unchanged" against the *previous
        Indigo reading* — and the accessory sits at the colour it had when the
        ramp started while the lamp walks arbitrarily far away from it. No
        error, no warning, nothing in the log at all.

        Against the last value actually pushed, the same tolerance bounds the
        TOTAL error at 1°, which is what a tolerance is supposed to mean.
        """
        devices.add(self._colour(devices, 0))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(300, "extendedColorLight")])
        h.start()

        for green in range(1, 11):
            h.bridge.device_updated(self._colour(devices, green - 1), self._colour(devices, green))

        hues = [call[2]["hue"] for call in h.client.calls
                if call[0] == "set_state" and "hue" in call[2]]
        assert hues, "a 6-degree ramp must reach the ecosystem"
        assert hues[-1] > 4, f"the last pushed hue must track the lamp, got {hues}"

    def test_a_state_equal_to_the_last_push_sends_nothing(
            self, bridge_mod, mock_logger, devices):
        """The other half: this must not become "push on every callback"."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        for _ in range(5):
            h.bridge.device_updated(DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40),
                                    DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40))
        assert h.client.names() == []

    def test_a_partial_push_is_merged_not_replaced(self, bridge_mod, mock_logger, devices):
        """§3.4 state maps are partial and the node leaves absent keys alone.

        A snapshot that replaced rather than merged would forget every key the
        last push happened not to carry, and re-send it on the next change.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.bridge.device_updated(DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40),
                                DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90))
        h.bridge.device_updated(DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90),
                                DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90))
        assert [call for call in h.client.calls if call[0] == "set_state"] == [
            ("set_state", 102, {"level": 90}),
        ], "onOff was already pushed by the attach and must not be re-sent"

    def test_the_snapshot_dies_with_the_export(self, bridge_mod, mock_logger, devices):
        """Bounded memory: entries are removed with the export, never accumulated."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        assert 101 in h.bridge._pushed
        h.store.remove(101)
        h.bridge.remove(101)
        assert h.bridge._pushed == {}

    def test_a_skipped_device_drops_its_snapshot(self, bridge_mod, mock_logger, devices):
        """A device we are not sending to has a snapshot that is stale by definition."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        assert 101 in h.bridge._pushed
        devices.drop(101)
        h.bridge.endpoint_specs()
        assert 101 not in h.bridge._pushed

    def test_a_CORRECTION_folds_into_the_snapshot_it_just_pushed(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """⊗ The unbounded-drift bug, coming back in through the correction path.

        `_correct` pushes the device's real state after a dispatch we could not
        apply, so the snapshot must record it. Otherwise the next diff is
        measured from a value the ecosystem no longer holds — which is exactly
        the fault the last-pushed snapshot exists to close.
        """
        dev = RelayDevice(123456789, "Golden Plug", onState=False)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        assert h.bridge._pushed[123456789] == {"onOff": False}

        # The command half-applies: dispatch raises, but the relay really moved.
        import export_handlers
        handler = export_handlers.handler_for("onOffLight")
        real_dispatch = handler.dispatch

        def boom(command, args, device, options):
            device.onState = True                # the device DID move
            raise RuntimeError("driver blew up after switching")

        handler.dispatch = boom
        try:
            h.bridge.on_command(
                bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        finally:
            handler.dispatch = real_dispatch

        assert h.bridge._pushed[123456789] == {"onOff": True}, (
            "the snapshot must mirror the corrective push the ecosystem received")

    def test_a_correction_is_gated_on_a_LIVE_client_like_every_other_push(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """⊗ `self.client` is not the same question as "can it take a frame".

        A halted or un-attached client accepts nothing (§1.1), so reaching past
        `_live_client` here posts a corrective push into a node that is serving
        nothing — and loses the one warning that says the ecosystem is now
        showing a command that failed.
        """
        dev = RelayDevice(123456789, "Golden Plug", onState=False)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.client.attached = False
        h.client.halted = True
        h.client.halted_reason = "version_skew"
        before = len([call for call in h.client.calls if call[0] == "set_state"])

        import export_handlers
        handler = export_handlers.handler_for("onOffLight")
        real_dispatch = handler.dispatch
        handler.dispatch = lambda *a: (_ for _ in ()).throw(RuntimeError("driver died"))
        try:
            h.bridge.on_command(
                bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        finally:
            handler.dispatch = real_dispatch

        after = len([call for call in h.client.calls if call[0] == "set_state"])
        assert after == before, "nothing may be pushed into a halted node"
        assert "HALTED" in warnings_of(mock_logger)


# ---------------------------------------------------------------------------
# ADR-0013 / issue #281 — a confirmed CT dispatch pushes its own commanded
# value, so a device that permanently clamps the colour temperature stops
# fighting Apple's adaptive-lighting loop.
# ---------------------------------------------------------------------------
class TestCommandedStatePush:
    def _ct_lamp(self, dev_id=800, **overrides):
        kwargs = dict(name="CT Lamp", onState=True, brightness=29, whiteLevel=29,
                      whiteTemperature=2700, supportsWhiteTemperature=True)
        kwargs.update(overrides)
        name = kwargs.pop("name")
        return DimmerDevice(dev_id, name, **kwargs)

    def _ct_count(self, h):
        return len([call for call in h.client.calls
                   if call[0] == "set_state" and "colorTempMireds" in call[2]])

    def test_a_confirmed_ct_dispatch_pushes_the_commanded_mireds(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        assert ("set_state", 800, {"colorTempMireds": 426}) in h.client.calls
        assert h.bridge._pushed[800]["colorTempMireds"] == 426

    def test_the_clamped_device_echo_after_a_commanded_push_sends_nothing(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """The live ~3s permanent re-assert issue #281 exists for: Apple wrote
        426 mireds, the z2m lamp's own warm limit clamped its echo to 400 —
        26 mireds short, forever, without the tolerance the commanded push
        relies on."""
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        before = self._ct_count(h)

        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))    # clamped → 400
        assert self._ct_count(h) == before

    def test_an_echo_far_outside_the_tolerance_still_pushes(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        before = self._ct_count(h)

        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=4000))    # a real change → 250
        assert self._ct_count(h) == before + 1

    def test_a_failed_ct_dispatch_pushes_truth_not_the_command(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        dev = self._ct_lamp()
        devices.add(dev)
        mock_indigo_base.dimmer.setColorLevels.side_effect = RuntimeError("driver blew up")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))

        pushes = [call[2]["colorTempMireds"] for call in h.client.calls
                 if call[0] == "set_state" and "colorTempMireds" in call[2]]
        assert 426 not in pushes, "a failed dispatch must never push the command it could not apply"
        assert pushes == [370], "the truth push (_correct) is the device's real, unchanged state"
        assert h.bridge._pushed[800]["colorTempMireds"] == 370

    def test_a_no_op_ct_dispatch_pushes_nothing(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        dev = self._ct_lamp(whiteLevel=None)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        assert [call for call in h.client.calls if call[0] == "set_state"] == []

    def test_a_commanded_push_is_gated_on_a_live_client(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """Mirrors the existing ``_correct`` live-client-gate test: `self.client`
        is not the same question as "can it take a frame"."""
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.client.attached = False
        h.client.halted = True
        h.client.halted_reason = "version_skew"
        before_pushed = dict(h.bridge._pushed[800])
        before = self._ct_count(h)

        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))

        assert self._ct_count(h) == before, "nothing may be pushed into a halted node"
        assert h.bridge._pushed[800] == before_pushed
        assert "HALTED" in warnings_of(mock_logger)

    def test_hue_returns_after_a_commanded_ct_detour(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """Kills "forgot to pass handler to _note_pushed": without eviction the
        snapshot still holds the pre-detour hue, so the device's return to it
        compares "unchanged" and the wire never hears about it again — the
        same #282 trap (b) ``TestColorModeCoherence`` pins for the
        ``device_updated`` path, reached here through a command instead.
        """
        hue_dev = DimmerDevice(810, "Strip", onState=True, brightness=100,
                               redLevel=0, greenLevel=0, blueLevel=100,
                               whiteLevel=50, whiteTemperature=None,
                               supportsRGB=True, supportsWhiteTemperature=True,
                               states={"colorMode": "hs"})
        devices.add(hue_dev)
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(810, "extendedColorLight")])
        h.start()                                          # attach pushes hue 240

        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=810, command="setColorTemp", args={"colorTempMireds": 400}))
        before_return = len(h.client.calls)

        h.bridge.device_updated(hue_dev, hue_dev)           # reports the SAME hue again
        new_pushes = h.client.calls[before_return:]
        hue_pushes = [call for call in new_pushes if call[0] == "set_state" and "hue" in call[2]]
        assert hue_pushes, "hue must be resent after the commanded CT detour evicted it"
        assert hue_pushes[-1][2]["hue"] == 240
        assert "saturation" in hue_pushes[-1][2], (
            "eviction removes ALL sibling mode-alternating keys, not hue alone")

    def test_the_adaptive_lighting_loop_terminates_after_one_round_trip(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """The issue #281 loop, driven three times over: Apple re-asserts 426,
        the lamp echoes its clamped 400 back. Once the commanded push has
        landed, every echo is absorbed by the tolerance — the wire never
        carries anything but 426 again. This is what makes the real Apple
        itself stop re-asserting: the node's reported attribute agrees with
        what it commanded, so there is nothing left to correct.
        """
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()

        for _ in range(3):
            h.bridge.on_command(bridge_protocol.BridgeCommand(
                indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
            h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))    # clamped echo

        assert mock_indigo_base.dimmer.setColorLevels.call_count == 3
        ct_pushes = [call[2]["colorTempMireds"] for call in h.client.calls
                    if call[0] == "set_state" and "colorTempMireds" in call[2]]
        assert set(ct_pushes) == {426}, (
            f"the clamped echo must never reach the wire once commanded, got {ct_pushes}")
        assert h.bridge._pushed[800]["colorTempMireds"] == 426


class TestCTBoundsLearnerWiring:
    """Issue #293: the learner is fed from real ``on_command``/``device_
    updated`` traffic, and a successful adoption republishes the endpoint —
    the ``ct_learner.py`` unit tests pin the state machine itself; these pin
    that ``ExportBridge`` actually wires it up.
    """

    def _ct_lamp(self, dev_id=800, **overrides):
        kwargs = dict(name="CT Lamp", onState=True, brightness=29, whiteLevel=29,
                      whiteTemperature=2700, supportsWhiteTemperature=True)
        kwargs.update(overrides)
        name = kwargs.pop("name")
        return DimmerDevice(dev_id, name, **kwargs)

    def test_commanded_push_is_not_clamped_to_the_effective_bounds(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """ADR-0013 pin, unchanged by #293: the pushed value is the command,
        clamped only to the GENERIC 153/500 domain, never to a narrower
        seeded/learned range — clamping it would stop the fabric from ever
        asking past the current bounds, which is exactly the overreach the
        learner needs to see."""
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [
            ExportEntry(800, "colorTemperatureLight",
                       options={"ctMinMireds": 200, "ctMaxMireds": 400})])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 450}))
        assert ("set_state", 800, {"colorTempMireds": 450}) in h.client.calls

    def test_two_consistent_shortfalls_learn_a_bound_and_republish(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        upserts_before = len([c for c in h.client.calls if c[0] == "upsert_endpoint"])

        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))   # echo -> 400
        # A second, distinct dispatch of the same ask — two confirmations
        # must answer two commands, not one command heard twice (#293
        # 2026-08-24 16:40 incident).
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))   # same echo again

        assert h.store.get(800).options["ctLearnedMaxMireds"] == 400
        upserts_after = len([c for c in h.client.calls if c[0] == "upsert_endpoint"])
        assert upserts_after == upserts_before + 1, "a learned bound must republish the endpoint"
        last_spec = [c for c in h.client.calls if c[0] == "upsert_endpoint"][-1][1]
        assert last_spec.options["ctMaxMireds"] == 400

    def test_duplicate_callbacks_off_one_dispatch_do_not_adopt(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """The incident pin at the wiring level (#293, 2026-08-24 16:40,
        device 1894385558, 2026.27.1 build): z2m can publish several
        attributes per state change, so ONE ``setColorTemp`` dispatch can
        fire ``device_updated`` more than once with the same lagged echo.
        Those duplicate callbacks must not satisfy the two-observations rule
        on their own — only a SECOND, distinct dispatch's matching echo may
        complete the streak."""
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()

        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))  # echo -> 400
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))  # duplicate callback
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))  # duplicate callback

        assert "ctLearnedMaxMireds" not in h.store.get(800).options

        # A second, distinct dispatch's matching echo still completes it.
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))

        assert h.store.get(800).options["ctLearnedMaxMireds"] == 400

    def test_a_single_shortfall_does_not_learn_anything(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))
        assert "ctLearnedMaxMireds" not in h.store.get(800).options

    def test_a_stale_commanded_reference_does_not_teach_the_learner(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """The degradation-path pin at the wiring level: an echo that matches
        an old command must not be adopted once the reference has gone
        stale, however the bridge is driven from the outside."""
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        # Simulate the reference going stale (an hour later) without a real
        # sleep — same discipline as every other latency-sensitive test in
        # this suite reaching into the harness's internals directly.
        commanded = h.bridge._ct_learner._commanded[800]
        h.bridge._ct_learner._commanded[800] = type(commanded)(mireds=commanded.mireds,
                                                                at=commanded.at - 3600)
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))
        assert "ctLearnedMaxMireds" not in h.store.get(800).options

    def test_a_reading_past_a_wrong_seed_re_widens_and_republishes(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """Self-healing: a deliberately-too-narrow seed is corrected the
        moment the device proves it reaches further, with no command
        involved at all — a plain ecosystem-independent Indigo-side read."""
        dev = self._ct_lamp(whiteTemperature=3333)   # -> ~300 mireds, past the seeded 250 ceiling
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [
            ExportEntry(800, "colorTemperatureLight", options={"ctMaxMireds": 250,
                                                                "ctMinMireds": 153})])
        h.start()
        upserts_before = len([c for c in h.client.calls if c[0] == "upsert_endpoint"])

        h.bridge.device_updated(dev, dev)   # re-report the same (out-of-bounds) reading

        assert h.store.get(800).options["ctLearnedMaxMireds"] == 300  # kelvin_to_mireds(3333)
        upserts_after = len([c for c in h.client.calls if c[0] == "upsert_endpoint"])
        assert upserts_after == upserts_before + 1

    def test_remove_forgets_the_learner_state(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """Issue #294 review — `remove()` must drop this device's `_commanded`/
        `_pending` learner state alongside its other per-device dicts, or a
        device_id later reused by a deleted-and-recreated Indigo device would
        inherit evidence about entirely different hardware."""
        dev = self._ct_lamp()
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(800, "colorTemperatureLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=800, command="setColorTemp", args={"colorTempMireds": 426}))
        h.bridge.device_updated(dev, self._ct_lamp(whiteTemperature=2500))  # pending: max, 400
        assert 800 in h.bridge._ct_learner._commanded
        assert 800 in h.bridge._ct_learner._pending

        h.bridge.remove(800)

        assert 800 not in h.bridge._ct_learner._commanded
        assert 800 not in h.bridge._ct_learner._pending


class TestColorModeCoherence:
    """Issue #282: the fix moves coherence for the node's colour-mode belief
    from a hardware write (zeroing the channel not in use) to the reporting
    side (publishing exactly one channel per snapshot). These pin the two
    traps that came with that move — see ``export_handlers.diff_from`` and
    ``ExportBridge._note_pushed`` for the mechanisms.
    """

    def _rgbw(self, dev_id=300, **overrides):
        kwargs = dict(onState=True, brightness=100, redLevel=0, greenLevel=0,
                      blueLevel=100, whiteTemperature=2700, supportsRGB=True,
                      supportsWhiteTemperature=True, states={})
        kwargs.update(overrides)
        return DimmerDevice(dev_id, "Strip", **kwargs)

    def test_hue_returning_after_a_CT_detour_is_resent_not_suppressed(
            self, bridge_mod, mock_logger, devices):
        """The load-bearing regression pin (trap (b)).

        push hue 240 (the attach) → device flips to CT mode, a push carries
        ``colorTempMireds`` → device returns to hue 240. A snapshot that only
        MERGES pushes still holds ``hue: 240`` from the first push, so the
        third event compares "unchanged" against itself and the hue push
        never goes out — the node stays on the CT channel, showing white for
        a colour the user just set again.
        """
        hue_dev = self._rgbw(redLevel=0, greenLevel=0, blueLevel=100,
                             whiteTemperature=None, states={"colorMode": "hs"})
        devices.add(hue_dev)
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(300, "extendedColorLight")])
        h.start()                                     # attach pushes hue 240

        ct_dev = self._rgbw(redLevel=0, greenLevel=0, blueLevel=0,
                            whiteTemperature=2700, states={"colorMode": "color_temp"})
        h.bridge.device_updated(hue_dev, ct_dev)       # the CT detour
        before_return = len(h.client.calls)

        h.bridge.device_updated(ct_dev, hue_dev)       # back to the SAME hue

        new_pushes = h.client.calls[before_return:]
        hue_pushes = [call for call in new_pushes
                      if call[0] == "set_state" and "hue" in call[2]]
        assert hue_pushes, (
            "hue must be re-sent when the device returns to it, even though "
            "the value is identical to what was pushed before the CT detour")
        assert hue_pushes[-1][2]["hue"] == 240
        assert "saturation" in hue_pushes[-1][2], (
            "the eviction removes ALL sibling mode-alternating keys, so "
            "saturation must be resent alongside hue, not just hue alone")

    def test_a_mode_flip_does_not_warn_but_a_genuinely_stopped_key_still_does(
            self, bridge_mod, mock_logger, devices):
        """Trap (a): ``_report_stopped_keys`` must not fire on every colour-
        mode flip ("stopped reporting hue, saturation" on a device that is
        working exactly as designed) — but a real stoppage of an unrelated
        key must still be reported, or the exemption has swallowed too much.
        """
        hue_dev = self._rgbw(states={"colorMode": "hs"})
        devices.add(hue_dev)
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(300, "extendedColorLight")])
        h.start()

        ct_dev = self._rgbw(redLevel=0, greenLevel=0, blueLevel=0,
                            whiteTemperature=2700, states={"colorMode": "color_temp"})
        h.bridge.device_updated(hue_dev, ct_dev)
        assert "stopped reporting" not in warnings_of(mock_logger), (
            "a colour-mode flip is not a device that stopped answering")

        dark_dev = self._rgbw(redLevel=0, greenLevel=0, blueLevel=0,
                              whiteTemperature=2700, brightness=None,
                              states={"colorMode": "color_temp"})
        h.bridge.device_updated(ct_dev, dark_dev)
        assert "stopped reporting" in warnings_of(mock_logger), (
            "a genuine stoppage of an unrelated key (level) must still warn")
        assert "level" in warnings_of(mock_logger)

    def test_attach_snapshot_carries_exactly_one_colour_channel(
            self, bridge_mod, mock_logger, devices):
        """The full-snapshot path (``_spec_for``) goes through ``states_for``
        too, so mode selection must apply there automatically — this is the
        proof, not an assumption.
        """
        dev = self._rgbw(redLevel=100, greenLevel=0, blueLevel=0,
                         whiteTemperature=2700, states={"colorMode": "hs"})
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(300, "extendedColorLight")])
        h.start()
        h.client.status = _migrate_status()
        h.bridge.reattach()

        _name, endpoints, _replace_all = h.client.only("attach")
        (spec,) = endpoints
        assert "hue" in spec.states and "saturation" in spec.states
        assert "colorTempMireds" not in spec.states


# ---------------------------------------------------------------------------
# E5 — §5 commands leave the loop
# ---------------------------------------------------------------------------
class TestCommandDispatchOffLoop:
    def test_the_indigo_call_runs_on_the_command_worker(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """The loop is SHARED with the inbound matter-server client.

        A blocking ``indigo.device.turnOn`` on it makes every live Matter device
        update wait behind somebody pressing a button in the Home app.
        """
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()

        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

        assert h.executor.submitted == 1
        mock_indigo_base.device.turnOn.assert_called_once()

    def test_a_command_for_an_unexported_device_costs_no_thread_hop(
            self, bridge_mod, mock_logger, devices):
        """The store and role lookups stay on the loop: they are dict reads."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        assert h.executor.submitted == 0
        assert "not exported" in warnings_of(mock_logger)

    def test_the_worker_is_shut_down_with_the_client(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        h.bridge.stop()
        # `wait=False`: a dispatch blocked on a wedged IndigoServer must not hold
        # plugin shutdown open. `cancel_futures=True`: work that has not STARTED
        # must not run against a plugin that is already tearing down.
        assert h.executor.shutdown_calls == [(False, True)]

    def test_a_queued_coroutine_cannot_rebuild_the_worker_after_stop(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """A stopped bridge must not leave a live worker thread behind it.

        ``_command_worker`` builds on first use, and a §5 command already queued
        on the loop when ``stop()`` ran would otherwise construct a brand-new
        ThreadPoolExecutor underneath a bridge that has shut down — one nothing
        ever joins.
        """
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.bridge.stop()

        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

        assert h.executor.submitted == 0
        assert h.bridge._executor is None


# ---------------------------------------------------------------------------
# E5 hardening (PR #126 dual-review batch)
# ---------------------------------------------------------------------------
class TestCommandWorkerHealth:
    """One wedged `indigo.*` call must not be a silent house-wide outage.

    The worker is deliberately single-threaded (per-device FIFO), and the price
    is that whatever is at the front blocks every command for every device. Left
    unbounded and unwatched that is an outage with NO log output at all: the
    frames keep arriving, the queue keeps growing, and from Indigo's side
    nothing happened.
    """

    def _harness(self, bridge_mod, mock_logger, devices, hang=False):
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        # Set AFTER start(): the attach's own provider run must not hang.
        h.executor.hang = hang
        return h

    def test_the_worker_is_single_threaded_on_purpose(self, bridge_mod):
        """FIFO is a CORRECTNESS property, not a resource budget.

        Two commands for the same accessory running concurrently lets a
        `setLevel 20` overtake a `setLevel 80` — both "succeed" and the lamp is
        left at the wrong brightness with nothing to correct it. A pool would
        pass every other test in this file.
        """
        executor = bridge_mod._command_executor()
        try:
            assert executor._max_workers == 1
        finally:
            executor.shutdown(wait=False)

    def test_dispatch_order_is_preserved_by_a_real_executor(self, bridge_mod):
        """The property the single worker exists for, against a real pool."""
        executor = bridge_mod._command_executor()
        order: list = []
        try:
            futures = [executor.submit(order.append, n) for n in range(25)]
            for future in futures:
                future.result(timeout=5)
        finally:
            executor.shutdown(wait=True)
        assert order == list(range(25))

    def test_a_wedged_dispatch_is_named_with_its_command_and_device(
            self, bridge_mod, mock_logger, devices, mock_indigo_base, monkeypatch):
        monkeypatch.setattr(bridge_mod, "COMMAND_TIMEOUT", 0.01)
        h = self._harness(bridge_mod, mock_logger, devices, hang=True)
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

        errors = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                          else str(call.args[0]) for call in mock_logger.error.call_args_list)
        assert "onOff" in errors and "123456789" in errors
        assert "has not returned" in errors

    def _push(self, h, times=1):
        for _ in range(times):
            h.bridge.on_command(
                bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

    def test_a_REAL_wedged_dispatch_raises_the_queue_depth(
            self, bridge_mod, mock_logger, devices, mock_indigo_base, monkeypatch):
        """⊗ The counters, driven through the actual dispatch path.

        Every other test here sets `_submitted`/`_completed` by hand, so nothing
        drove them through `_dispatch_off_loop` — and deleting `self._submitted
        += 1` left the "commands are stuck" warning permanently unreachable in
        production while the suite stayed green. That warning is the ONLY
        symptom of a wedged `indigo.*` call: every command still arrives,
        nothing raises, and the house simply stops answering the Home app.
        """
        monkeypatch.setattr(bridge_mod, "COMMAND_TIMEOUT", 0.01)
        monkeypatch.setattr(bridge_mod, "COMMAND_QUEUE_WARN", 2)
        h = self._harness(bridge_mod, mock_logger, devices, hang=True)

        self._push(h, 3)
        h.bridge.health_tick()

        assert "queued on the command worker" in warnings_of(mock_logger)

    def test_commands_that_COMPLETED_do_not_read_as_a_stuck_queue(
            self, bridge_mod, mock_logger, devices, mock_indigo_base, monkeypatch):
        """⊗ The other half of the same accounting.

        `_completed` was also only ever set by hand, so deleting its increment
        left a permanent false "commands are stuck" alarm on a bridge that was
        working perfectly — and nothing noticed.
        """
        monkeypatch.setattr(bridge_mod, "COMMAND_QUEUE_WARN", 2)
        h = self._harness(bridge_mod, mock_logger, devices)

        self._push(h, 4)
        h.bridge.health_tick()

        assert "queued on the command worker" not in warnings_of(mock_logger)

    def test_the_queue_depth_warning_fires_once_per_streak(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        h = self._harness(bridge_mod, mock_logger, devices)
        # Stand in for work handed to a worker that is not coming back.
        h.bridge._submitted = bridge_mod.COMMAND_QUEUE_WARN

        h.bridge.health_tick()
        h.bridge.health_tick()

        queued = [call for call in mock_logger.warning.call_args_list
                  if "queued on the command worker" in str(call.args[0])]
        assert len(queued) == 1, "a standing queue must not repeat every 15s"

        # ...and it re-arms once the queue drains, so a NEW pile-up is news.
        h.bridge._completed = h.bridge._submitted
        h.bridge.health_tick()
        h.bridge._submitted += bridge_mod.COMMAND_QUEUE_WARN
        h.bridge.health_tick()
        queued = [call for call in mock_logger.warning.call_args_list
                  if "queued on the command worker" in str(call.args[0])]
        assert len(queued) == 2

    def test_stop_reports_how_many_commands_were_dropped(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._submitted = 4
        h.bridge._completed = 1
        h.bridge._executor = h.executor

        h.bridge.stop()

        assert "3 ecosystem command(s) still queued" in warnings_of(mock_logger)


class TestNodeWarnings:
    """§4.3 `warnings` — the node's only route to a log a human reads.

    The node writes to stdout and, in this milestone, is started BY HAND, so its
    stdout is a terminal that closed hours ago. A map it could not write is the
    exact fault E5 exists to make visible.
    """

    def _status(self, warnings):
        return bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 0,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": warnings,
        })

    def test_a_reported_warning_reaches_the_indigo_log(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge._on_attached(self._status(["Could not write the endpoint map"]), False)
        assert "Could not write the endpoint map" in warnings_of(mock_logger)

    def test_a_standing_warning_is_said_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        status = self._status(["disk full"])
        h.bridge._on_attached(status, False)
        h.bridge._on_attached(status, False)
        said = [call for call in mock_logger.warning.call_args_list
                if "disk full" in str(call.args)]
        assert len(said) == 1

    def test_a_new_warning_beside_a_standing_one_is_still_news(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge._on_attached(self._status(["disk full"]), False)
        h.bridge._on_attached(self._status(["disk full", "identity unwritable"]), False)
        assert "identity unwritable" in warnings_of(mock_logger)

    def test_an_empty_warnings_list_says_nothing_and_re_arms(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge._on_attached(self._status(["disk full"]), False)
        h.bridge._on_attached(self._status([]), False)
        h.bridge._on_attached(self._status(["disk full"]), False)
        said = [call for call in mock_logger.warning.call_args_list
                if "disk full" in str(call.args)]
        assert len(said) == 2, "a fault that recurs after clearing is news again"


class TestNodeWarningsAreActuallyPolled:
    """⊗ The §4.3 warnings channel had NO reader at all.

    Four docstrings and BRIDGE_PROTOCOL §4.3 all said `get_status` is polled,
    and nothing polled it: `_report_node_warnings` ran on the attach response
    and nowhere else, while `BridgeClient.get_status` had zero production
    callers. Three of the four faults the channel was built for cannot happen at
    attach time, so they reached the user as nothing at all —

    * the identity witness write on FIRST commissioning, which happens when a
      fabric appears, long after the attach;
    * the witness clear on `factory_reset`, whose failure makes the very next
      start refuse to serve and blame lost storage for the reset the user asked
      for;
    * the endpoint-map write from `upsert`/`remove`'s drift check, which is a
      full disk quietly costing the ability to notice a mass renumbering.
    """

    def _status(self, warnings):
        return bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": warnings,
        })

    def _attached(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.client.attached = True
        return h

    def test_the_watchdog_asks_the_node_how_it_is(self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.bridge.health_tick()
        assert "get_status" in h.client.names()

    def test_a_fault_that_appears_AFTER_the_attach_still_reaches_the_log(
            self, bridge_mod, mock_logger, devices):
        """The whole point: these faults happen while the socket is up."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status([]), False)      # clean at attach time
        h.client.status = self._status(
            ["Could not record the commissioning marker in identity.json"])

        h.bridge.health_tick()

        assert "commissioning marker" in warnings_of(mock_logger)

    def test_a_standing_fault_is_not_repeated_every_15_seconds(
            self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(["disk full"])
        for _ in range(5):
            h.bridge.health_tick()
        said = [call for call in mock_logger.warning.call_args_list
                if "disk full" in str(call.args)]
        assert len(said) == 1, "a full disk does not un-fill; say it once per streak"

    def test_a_poll_that_fails_is_not_itself_news(self, bridge_mod, mock_logger, devices):
        """The socket being gone is what `_disconnect_ticks` is for."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.fail["get_status"] = ConnectionError("node went away")
        h.bridge.health_tick()      # must not raise
        assert "status poll failed" not in warnings_of(mock_logger)

    def test_a_halted_or_recovering_node_is_not_polled(
            self, bridge_mod, mock_logger, devices):
        """Both have already said the one thing that matters, at error level."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.recovery = True
        h.client.attached = False
        h.bridge.health_tick()
        assert "get_status" not in h.client.names()


class TestSubscriptionHealthDevice:
    """§4.3 issue #286 — `matterBridgeHealth`'s states, driven by the node's
    `subscriptionChurn` verdict.

    The warning SENTENCE for an active verdict already rides `warnings`
    (`TestNodeWarnings` above); what is new here is the trigger-able DEVICE
    STATE it drives, and the degradation path: `checked=False` — an old node,
    a halted/detached client, an unreachable node — must read `unknown`,
    never `healthy`. `unknown` is the "did not observe" answer, and a test
    named `..._returns_healthy_when_unchecked` would have pinned the bug this
    device exists to avoid.
    """

    HEALTHY_CHURN = {"checked": True, "active": False, "peers": []}
    CHURNING_CHURN = {
        "checked": True, "active": True,
        "peers": [{"peerNodeId": "41869fbd537ef01", "fabricIndex": 2, "liveSessions": 5,
                    "invalidDeletions": 3, "windowMinutes": 30,
                    "since": "2026-08-23T09:12:00.000Z"}],
    }

    def _status(self, churn=None):
        payload = {
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
        }
        if churn is not None:
            payload["subscriptionChurn"] = churn
        return bridge_protocol.parse_status(payload)

    def _harness(self, bridge_mod, mock_logger, devices) -> Harness:
        return Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])

    def _attached(self, bridge_mod, mock_logger, devices) -> Harness:
        """Attached via the watchdog's OWN poll path, not `_on_attached` —
        the shape `TestNodeWarningsAreActuallyPolled` uses, so `health_tick`'s
        polling (rather than a bare method call) is what is under test.
        """
        h = self._harness(bridge_mod, mock_logger, devices)
        h.start()
        h.client.attached = True
        return h

    def test_the_device_is_created_on_attach_when_export_is_enabled(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)
        assert len(h.health_devices) == 1
        assert h.health_devices[0].states["subscriptionHealth"] == "healthy"
        assert h.health_devices[0].states["churnDetail"] == ""

    def test_no_client_never_creates_the_health_device(
            self, bridge_mod, mock_logger, devices):
        """`health_tick` with nothing exported (or not yet attached) has no
        churn verdict to report and must not create a device just to say so."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge.health_tick()
        assert h.health_devices == []

    def test_churn_active_flips_the_state_to_churning_with_peer_detail(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "churning"
        assert "41869fbd537ef01" in dev.states["churnDetail"]
        assert "fabric 2" in dev.states["churnDetail"]
        # The warning sentence is the OTHER channel's job (TestNodeWarnings);
        # a churn detection must not ALSO log through this one.
        assert "recovered" not in infos_of(mock_logger)

    def test_recovery_flips_back_to_healthy_and_logs_once(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "healthy"
        assert dev.states["churnDetail"] == ""
        assert "recovered" in infos_of(mock_logger)

    def test_a_reattach_after_unknown_does_not_claim_a_recovery(
            self, bridge_mod, mock_logger, devices):
        """healthy -> unknown (node halted) -> healthy again is an ordinary
        node restart, not the end of a churn episode — "recovered" would be a
        false signal in a log a user is reading precisely because something
        restarted."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.HEALTHY_CHURN)
        h.bridge.health_tick()
        h.client.halted = True
        h.bridge.health_tick()
        h.client.halted = False
        h.bridge.health_tick()
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "healthy"
        assert "recovered" not in infos_of(mock_logger)

    def test_standing_churn_across_five_ticks_writes_and_logs_once(
            self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.CHURNING_CHURN)
        for _ in range(5):
            h.bridge.health_tick()
        assert len(h.health_devices) == 1, "the device is resolved once, not recreated per tick"
        dev = h.health_devices[0]
        assert len(dev.writes) == 1, "a standing verdict must not be rewritten every ~15s tick"
        assert dev.states["subscriptionHealth"] == "churning"

    def test_a_halted_node_reads_unknown(self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.CHURNING_CHURN)
        h.bridge.health_tick()
        h.client.halted = True
        h.bridge.health_tick()
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "unknown"
        assert dev.states["churnDetail"] == ""

    def test_a_detached_node_reads_unknown(self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.CHURNING_CHURN)
        h.bridge.health_tick()
        h.client.attached = False
        h.bridge.health_tick()
        assert h.health_devices[0].states["subscriptionHealth"] == "unknown"

    def test_status_missing_the_field_creates_no_device_when_none_exists(
            self, bridge_mod, mock_logger, devices):
        """An old (pre-0.15.0) node's `StatusReport` has no `subscriptionChurn`
        key at all — `bridge_protocol.parse_status` already defaults that to
        `checked=False`. The FIRST thing this plugin ever hears from a node is
        "unknown" — and unknown must never be the reason a device gets
        created (review finding 1): there is nothing real yet to correct."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(churn=None), False)
        assert h.health_devices == []

    def test_status_missing_the_field_corrects_an_existing_device_to_unknown(
            self, bridge_mod, mock_logger, devices):
        """...but a device that already exists — created earlier THIS session
        by a real signal — must still be corrected, never left standing on a
        stale "healthy", and never flipped to "healthy" by the absence of the
        field itself."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)   # creates it, healthy
        h.bridge._on_attached(self._status(churn=None), False)           # old-node status now
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "unknown"
        assert dev.states["subscriptionHealth"] != "healthy"

    def test_a_poll_failure_does_not_raise_or_create_the_device(
            self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.fail["get_status"] = ConnectionError("node went away")
        h.bridge.health_tick()      # must not raise
        assert h.health_devices == []

    def test_a_device_creation_failure_does_not_break_the_poll_loop(
            self, bridge_mod, mock_logger, devices):
        """Never raises, and is said once (the same latch shape as
        `_report_node_warnings`) rather than on every subsequent change."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.health_device_fail = True

        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)  # must not raise

        assert "could not find or create the bridge health device" in warnings_of(mock_logger)
        said = len(mock_logger.warning.call_args_list)
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)   # still failing, still must not raise
        assert len(mock_logger.warning.call_args_list) == said, "a standing failure is said once"

    def test_export_switched_off_reads_unknown(self, bridge_mod, mock_logger, devices):
        """PRD §5.5 — export off disconnects but leaves accessories paired;
        the bridge's OWN health is no longer something this plugin can speak
        to, so it degrades exactly as a halted node does."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.CHURNING_CHURN)
        h.bridge.health_tick()
        h.prefs["exportEnabled"] = False

        h.bridge.exports_changed()

        assert h.health_devices[0].states["subscriptionHealth"] == "unknown"

    def test_a_recovering_node_reads_unknown(self, bridge_mod, mock_logger, devices):
        """review finding 6a — the ONE `_mark_health_unknown` call site
        `client.recovery` guards was untested; mirrors
        `test_a_halted_node_reads_unknown`."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.CHURNING_CHURN)
        h.bridge.health_tick()
        h.client.recovery = True
        h.client.attached = False
        h.bridge.health_tick()
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "unknown"


class TestFabricSlots:
    """§4.3 issue #288 — `matterBridgeHealth`'s five positional per-fabric
    slots.

    `subscriptionHealth` alone can say "churning" but not WHICH paired
    ecosystem is churning once a house has more than one. Slots are
    POSITIONAL — sorted by `fabric_index`, packed 1..N — not tied to a real
    fabric's index, so a fabric leaving repacks the rest rather than leaving
    a gap (Simon's explicit design call, issue #288).
    """

    #: Real DCL vendor ids (`export_bridge.VENDOR_NAMES`) with EMPTY labels —
    #: issue #288 review finding A: Apple/Alexa/Google never call
    #: `UpdateFabricLabel`, so a real fabric's label is blank on Simon's own
    #: rig, and slot names must be built vendor-first or every slot would
    #: read "fabric 1 / fabric 8 / fabric 9". `test_a_labelled_fabric_gets_
    #: the_vendor_prefixed_suffix` pins the one case a label DOES show up.
    APPLE = {"fabricIndex": 1, "label": "", "vendorId": 0x1349}
    ALEXA = {"fabricIndex": 2, "label": "", "vendorId": 0x1217}

    def _churn(self, peers=(), active=None):
        """``active`` defaults to ``bool(peers)`` but is overridable — issue
        #288 review finding B needs an ACTIVE verdict whose peers name no
        connected fabric at all, which ``bool(peers)`` alone cannot build
        once ``peers`` itself is empty."""
        return {"checked": True, "active": bool(peers) if active is None else active,
                "peers": list(peers)}

    def _peer(self, fabric_index):
        return {"peerNodeId": "41869fbd537ef01", "fabricIndex": fabric_index, "liveSessions": 5,
                "invalidDeletions": 3, "windowMinutes": 30, "since": "2026-08-23T09:12:00.000Z"}

    def _status(self, fabrics=(), churn=None):
        payload = {
            "commissioned": True, "fabrics": list(fabrics), "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
        }
        if churn is not None:
            payload["subscriptionChurn"] = churn
        return bridge_protocol.parse_status(payload)

    def _harness(self, bridge_mod, mock_logger, devices) -> Harness:
        return Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])

    def _attached(self, bridge_mod, mock_logger, devices) -> Harness:
        h = self._harness(bridge_mod, mock_logger, devices)
        h.start()
        h.client.attached = True
        return h

    def test_two_fabrics_fill_the_first_two_slots_healthy(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(
            self._status([self.APPLE, self.ALEXA], self._churn()), False)
        dev = h.health_devices[0]
        assert dev.states["fabric1Name"] == "Apple Home"
        assert dev.states["fabric1Health"] == "healthy"
        assert dev.states["fabric2Name"] == "Amazon Alexa"
        assert dev.states["fabric2Health"] == "healthy"
        for slot in (3, 4, 5):
            assert dev.states[f"fabric{slot}Name"] == ""
            assert dev.states[f"fabric{slot}Health"] == ""

    def test_a_fabric_with_no_vendor_or_label_falls_back_to_its_index(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(
            self._status([{"fabricIndex": 7, "label": "", "vendorId": 0}], self._churn()), False)
        assert h.health_devices[0].states["fabric1Name"] == "fabric 7"

    def test_an_unrecognised_vendor_falls_back_to_the_hex_form(
            self, bridge_mod, mock_logger, devices):
        """review finding A: ``vendor 0x%04X``, not a bare (and blank) label —
        the only thing that can distinguish two unrecognised-vendor fabrics
        from each other when both leave the label empty, as real ones do."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(
            self._status([{"fabricIndex": 3, "label": "", "vendorId": 0x9999}],
                         self._churn()), False)
        assert h.health_devices[0].states["fabric1Name"] == "vendor 0x9999"

    def test_a_labelled_fabric_gets_the_vendor_prefixed_suffix(
            self, bridge_mod, mock_logger, devices):
        """review finding A: vendor FIRST, the label appended only as a
        suffix — never the label alone, which two Apple fabrics with the
        same blank label could never tell apart."""
        h = self._harness(bridge_mod, mock_logger, devices)
        labelled = {"fabricIndex": 1, "label": "Simon's House", "vendorId": 0x1349}
        h.bridge._on_attached(self._status([labelled], self._churn()), False)
        assert h.health_devices[0].states["fabric1Name"] == "Apple Home — Simon's House"

    def test_churn_on_one_fabric_flips_only_that_slots_health(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        churn = self._churn([self._peer(2)])   # fabric 2 (Alexa) churning
        h.bridge._on_attached(self._status([self.APPLE, self.ALEXA], churn), False)
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "churning"
        assert dev.states["fabric1Health"] == "healthy"
        assert dev.states["fabric2Health"] == "churning"

    def test_unattributable_active_churn_marks_fitted_slots_unknown_not_healthy(
            self, bridge_mod, mock_logger, devices):
        """review finding B: an ACTIVE verdict whose only peer names a
        fabric_index that is not one of the connected (FITTED) fabrics must
        not assert "healthy" on slots it never actually cleared — the
        churn is real, only its attribution is missing."""
        h = self._harness(bridge_mod, mock_logger, devices)
        churn = self._churn([self._peer(99)])   # fabric 99 is not connected at all
        h.bridge._on_attached(self._status([self.APPLE, self.ALEXA], churn), False)
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "churning"
        assert dev.states["fabric1Health"] == "unknown"
        assert dev.states["fabric2Health"] == "unknown"
        assert dev.states["fabric1Health"] != "healthy"

    def test_active_with_no_peers_at_all_marks_fitted_slots_unknown(
            self, bridge_mod, mock_logger, devices):
        """review finding B: `active=True` with an EMPTY peers list — every
        entry was malformed and dropped by `_parse_churn_peer` — is the
        other unattributable shape `bool(peers)` alone cannot construct."""
        h = self._harness(bridge_mod, mock_logger, devices)
        churn = self._churn([], active=True)
        h.bridge._on_attached(self._status([self.APPLE, self.ALEXA], churn), False)
        dev = h.health_devices[0]
        assert dev.states["fabric1Health"] == "unknown"
        assert dev.states["fabric2Health"] == "unknown"

    def test_unattributable_churn_on_a_dropped_sixth_fabric_also_demotes(
            self, bridge_mod, mock_logger, devices):
        """review finding B: a churning peer whose fabric is the 6th
        (DROPPED, not fitted into any slot) is exactly as unattributable to
        a VISIBLE slot as a garbage index — same demotion."""
        six = [{"fabricIndex": i, "label": "", "vendorId": 0x9990 + i} for i in range(1, 7)]
        churn = self._churn([self._peer(6)])   # the dropped fabric is the one churning
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(six, churn), False)
        dev = h.health_devices[0]
        assert dev.states["subscriptionHealth"] == "churning"
        for slot in range(1, 6):
            assert dev.states[f"fabric{slot}Health"] == "unknown"

    def test_unchecked_verdict_marks_occupied_slots_unknown_never_healthy(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(
            self._status([self.APPLE, self.ALEXA], self._churn()), False)  # both healthy
        h.bridge._on_attached(self._status(churn=None), False)             # old-node status now

        dev = h.health_devices[0]
        assert dev.states["fabric1Health"] == "unknown"
        assert dev.states["fabric2Health"] == "unknown"
        assert dev.states["fabric1Health"] != "healthy"
        # Names retained -- visibility while unobserved is the point.
        assert dev.states["fabric1Name"] == "Apple Home"
        assert dev.states["fabric2Name"] == "Amazon Alexa"

    def test_a_fabric_leaving_repacks_slots_and_vacates_the_tail(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(
            self._status([self.APPLE, self.ALEXA], self._churn()), False)
        h.bridge._on_attached(self._status([self.APPLE], self._churn()), False)  # Alexa left

        dev = h.health_devices[0]
        assert dev.states["fabric1Name"] == "Apple Home"
        assert dev.states["fabric2Name"] == ""
        assert dev.states["fabric2Health"] == ""

    def test_a_fabric_leaving_pulls_the_survivor_into_slot_one(
            self, bridge_mod, mock_logger, devices):
        """review finding F.1 — the sticky-slot mutant: slot 1 must become
        whichever fabric is now lowest by index, not stay whatever happened
        to occupy slot 1 before."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status([self.APPLE, self.ALEXA], self._churn()), False)
        h.bridge._on_attached(self._status([self.ALEXA], self._churn()), False)  # Apple left

        dev = h.health_devices[0]
        assert dev.states["fabric1Name"] == "Amazon Alexa"
        assert dev.states["fabric2Name"] == ""

    def test_fabrics_are_packed_by_index_not_by_arrival_order(
            self, bridge_mod, mock_logger, devices):
        """review finding F.2 — kills deleting the `sorted()` call: fed as
        [Alexa (index 2), Apple (index 1)], slot 1 must still be Apple."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status([self.ALEXA, self.APPLE], self._churn()), False)
        dev = h.health_devices[0]
        assert dev.states["fabric1Name"] == "Apple Home"
        assert dev.states["fabric2Name"] == "Amazon Alexa"

    def test_six_fabrics_fill_five_and_warn_once_naming_the_dropped_one(
            self, bridge_mod, mock_logger, devices):
        fabrics = [{"fabricIndex": i, "label": "", "vendorId": 0x9990 + i}
                   for i in range(1, 7)]
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(fabrics, self._churn())

        h.bridge.health_tick()

        dev = h.health_devices[0]
        for slot in range(1, 6):
            assert dev.states[f"fabric{slot}Name"] == f"vendor 0x{0x9990 + slot:04X}"
        assert f"vendor 0x{0x9990 + 6:04X}" in warnings_of(mock_logger)
        said = len(mock_logger.warning.call_args_list)

        h.bridge.health_tick()   # the same six fabrics again -- must not re-log

        assert len(mock_logger.warning.call_args_list) == said

    def test_dropped_fabrics_warning_clears_once_the_overflow_ends(
            self, bridge_mod, mock_logger, devices):
        six = [{"fabricIndex": i, "label": "", "vendorId": 0x9990 + i} for i in range(1, 7)]
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(six, self._churn())
        h.bridge.health_tick()
        said = len(mock_logger.warning.call_args_list)

        h.client.status = self._status(six[:5], self._churn())   # back to five -- no overflow
        h.bridge.health_tick()
        h.client.status = self._status(six, self._churn())       # overflows again
        h.bridge.health_tick()

        assert len(mock_logger.warning.call_args_list) == said + 1, \
            "a cleared-then-recurring overflow is news again"

    def test_overflow_set_changing_without_ever_emptying_still_re_warns(
            self, bridge_mod, mock_logger, devices):
        """review finding F.5 — {6} -> {7} (fabric 6 leaves, fabric 7 takes
        its place; the COUNT of connected fabrics never dips below six) must
        still be news: kills weakening "did the DROPPED SET change" down to
        a mere "is something dropped" check."""
        base = [{"fabricIndex": i, "label": "", "vendorId": 0x9990 + i} for i in range(1, 7)]
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(base, self._churn())
        h.bridge.health_tick()
        said = len(mock_logger.warning.call_args_list)

        swapped = base[:5] + [{"fabricIndex": 7, "label": "", "vendorId": 0x9999}]
        h.client.status = self._status(swapped, self._churn())
        h.bridge.health_tick()

        assert len(mock_logger.warning.call_args_list) == said + 1
        assert "vendor 0x9999" in warnings_of(mock_logger)

    def test_a_restart_with_persisted_slot_states_resets_healths_keeps_names(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        stale = FakeHealthDevice(9600)
        stale.states.update({
            "subscriptionHealth": "healthy", "churnDetail": "",
            "fabric1Name": "Apple Home", "fabric1Health": "healthy",
            "fabric2Name": "Amazon Alexa", "fabric2Health": "churning",
            "fabric3Name": "", "fabric3Health": "",
            "fabric4Name": "", "fabric4Health": "",
            "fabric5Name": "", "fabric5Health": "",
        })
        h.health_devices.append(stale)   # persisted from a PREVIOUS session

        h.bridge.health_tick()           # fresh ExportBridge; self.client is None

        assert stale.states["subscriptionHealth"] == "unknown"
        assert stale.states["fabric1Health"] == "unknown"
        assert stale.states["fabric2Health"] == "unknown"
        assert stale.states["fabric1Name"] == "Apple Home"
        assert stale.states["fabric2Name"] == "Amazon Alexa"
        assert stale.states["fabric3Health"] == "", "a vacant slot stays vacant, not \"unknown\""
        assert len(h.health_devices) == 1, "must FIND the existing device, never create a second"

    def test_standing_slot_state_across_five_ticks_writes_once(
            self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status([self.APPLE, self.ALEXA], self._churn())
        for _ in range(5):
            h.bridge.health_tick()
        dev = h.health_devices[0]
        assert len(dev.writes) == 1, "a standing slot plan must not be rewritten every ~15s tick"

    def test_standing_unchecked_verdict_across_five_ticks_writes_once(
            self, bridge_mod, mock_logger, devices):
        """review finding F.4 — the UNCHECKED branch's own no-change gate,
        pinned on its own rather than only alongside a CHECKED write."""
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status([self.APPLE, self.ALEXA], self._churn())
        h.bridge.health_tick()          # healthy, device created (write 1)
        dev = h.health_devices[0]
        h.client.halted = True
        h.bridge.health_tick()          # flips to unknown (write 2)
        writes_so_far = len(dev.writes)

        for _ in range(5):
            h.bridge.health_tick()      # standing halted/unknown

        assert len(dev.writes) == writes_so_far, \
            "a standing UNCHECKED verdict must not be rewritten every ~15s tick"


class TestFabricSlotFinderThrottle:
    """§4.3 issue #288 review finding C.

    Contract implemented: the UNCHECKED branch resolves the health device
    through `_find_health_device_for_unknown`, which (1) uses the cheap
    cached-id `device_getter` round trip whenever `_health_device_id` is
    already known: (2) otherwise performs the full
    `indigo.devices.iter("self")` scan AT MOST ONCE PER PLUGIN SESSION —
    exactly the one scan issue #286's restart-reconcile needs — and
    remembers the answer via `_health_reconcile_scanned`, found or not, so a
    bridge that never attaches does not pay a fresh scan on every ~15s
    watchdog tick forever; and (3) every Indigo read the branch makes (that
    resolution AND the device's own `.states`) is inside ONE try/except in
    `_apply_subscription_churn`, latched via `_health_read_warned`, so a read
    failure degrades — logged once — instead of propagating out of
    `health_tick` on the watchdog thread. A CHECKED verdict's own
    `_ensure_health_device` (via the unthrottled `_find_health_device`) is
    NOT gated by any of this — it may still scan/create on every call.
    """

    def _harness(self, bridge_mod, mock_logger, devices) -> Harness:
        return Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])

    def test_five_no_device_unchecked_ticks_perform_exactly_one_finder_scan(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        for _ in range(5):
            h.bridge.health_tick()      # self.client is None every time
        assert h.health_device_finder_calls == 1
        assert h.health_devices == []

    def test_a_raising_device_read_warns_once_and_does_not_propagate(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.health_device_finder_raises = ConnectionError("IndigoServer went away")

        h.bridge.health_tick()          # must not raise
        h.bridge.health_tick()          # must not raise, and not re-warn

        assert "could not read the bridge health device" in warnings_of(mock_logger)
        said = [call for call in mock_logger.warning.call_args_list
                if "could not read the bridge health device" in str(call.args[0])]
        assert len(said) == 1

    def test_the_no_device_sentinel_is_distinct_from_a_real_all_vacant_device(
            self, bridge_mod, mock_logger, devices):
        """review finding F.3 — pins that the internal
        `(unknown, "", None, None)` "no device at all" sentinel is never
        confused with a real device's genuinely all-vacant state. Once the
        one scan this session finds nothing, a device that appears
        afterwards (out of band) is not looked for again by this throttled
        path — only a later CHECKED verdict would find it — so it must be
        left completely untouched, not silently treated as already handled.
        """
        h = self._harness(bridge_mod, mock_logger, devices)

        h.bridge.health_tick()          # no client at all -- the one scan, finds nothing
        assert h.bridge._health_state == (bridge_mod.HEALTH_UNKNOWN, "", None, None)
        assert h.health_devices == []

        # A device now exists (out of band) -- persisted "healthy", vacant slots.
        late = FakeHealthDevice(9700)
        late.states["subscriptionHealth"] = "healthy"
        late.states["churnDetail"] = ""
        for slot in range(1, 6):
            late.states[f"fabric{slot}Name"] = ""
            late.states[f"fabric{slot}Health"] = ""
        h.health_devices.append(late)

        h.bridge.health_tick()          # the one-scan throttle means this is NOT found

        assert h.bridge._health_state == (bridge_mod.HEALTH_UNKNOWN, "", None, None), (
            "the sentinel must not be confused with (or replaced by) a real all-vacant reading")
        assert late.writes == [], "an out-of-band device the throttled path never found stays untouched"
        assert late.states["subscriptionHealth"] == "healthy", "left exactly as found -- not corrected"



class TestStalePersistedHealthAcrossARestart:
    """§4.3 issue #286 review finding 1 — the bug a green suite hid.

    `_mark_health_unknown` used to gate on the IN-MEMORY `_health_device_id`
    cache. After a plugin restart that cache is always empty, so every
    halted/recovering/detached/no-client tick was a no-op — a device that
    read "healthy" (or "churning") at the moment the plugin last stopped
    stood exactly that way, with zero observation behind it, for as long as
    the new session took to get a REAL signal (an attach, or a successful
    poll). That is precisely the quiet-healthy this feature exists to
    forbid; a test asserting only "no client -> nothing happens" would have
    blessed it as intended behaviour.
    """

    def _harness(self, bridge_mod, mock_logger, devices) -> Harness:
        return Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])

    def _stale_device(self, health="healthy") -> FakeHealthDevice:
        dev = FakeHealthDevice(9500)
        dev.states["subscriptionHealth"] = health
        dev.states["churnDetail"] = ""
        return dev

    def test_first_health_tick_with_no_client_corrects_a_stale_healthy_device(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        stale = self._stale_device()
        h.health_devices.append(stale)   # persisted from a PREVIOUS session

        h.bridge.health_tick()           # fresh ExportBridge; self.client is None

        assert stale.states["subscriptionHealth"] == "unknown"
        assert len(h.health_devices) == 1, "must FIND the existing device, never create a second"

    def test_a_halted_node_on_a_fresh_bridge_corrects_a_stale_healthy_device(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        stale = self._stale_device()
        h.health_devices.append(stale)
        h.start()
        h.client.attached = True
        h.client.halted = True

        h.bridge.health_tick()

        assert stale.states["subscriptionHealth"] == "unknown"

    def test_mark_unknown_never_recreates_a_device_the_user_deleted(
            self, bridge_mod, mock_logger, devices):
        """The cached-id -> factory fall-through `_mark_health_unknown` used
        to have contradicted its own "only resets, never creates" docstring:
        once the cache was cleared (the device gone), the OLD code's shared
        `_ensure_health_device` call would recreate it. The find-only path
        must not."""
        h = self._harness(bridge_mod, mock_logger, devices)
        h.start()
        h.bridge._on_attached(bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
            "subscriptionChurn": {"checked": True, "active": False, "peers": []},
        }), False)                                     # creates it, healthy
        assert len(h.health_devices) == 1
        h.devices.drop(h.bridge._health_device_id)
        h.health_devices.clear()                        # the user deleted it out-of-band
        h.client.halted = True

        h.bridge.health_tick()                           # must not recreate it

        assert h.health_devices == []


class TestConsecutivePollFailuresMarkUnknown:
    """§4.3 issue #286 review finding 2.

    ``client.attached`` stays true for a node whose SOCKET is up but whose
    responses have simply stopped coming — the wedged case — so
    ``_disconnect_ticks`` (attach state) never fires, and a poll failure alone
    only ever reached DEBUG. A device left on its last reading (typically
    "healthy") is exactly the stale-good-news case this feature exists to
    prevent.
    """

    def _status(self, churn):
        return bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
            "subscriptionChurn": churn,
        })

    HEALTHY_CHURN = {"checked": True, "active": False, "peers": []}

    def _attached(self, bridge_mod, mock_logger, devices) -> Harness:
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.client.attached = True
        return h

    def test_n_consecutive_failures_mark_the_stale_value_unknown(
            self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.HEALTHY_CHURN)
        h.bridge.health_tick()                                     # healthy, device created
        assert h.health_devices[0].states["subscriptionHealth"] == "healthy"

        h.client.fail["get_status"] = ConnectionError("node went quiet")
        for _ in range(bridge_mod.HEALTH_POLL_FAIL_TICKS - 1):
            h.bridge.health_tick()                                 # not yet N — must not raise
            assert h.health_devices[0].states["subscriptionHealth"] == "healthy"
        h.bridge.health_tick()                                     # the Nth failure

        assert h.health_devices[0].states["subscriptionHealth"] == "unknown"

    def test_a_successful_poll_resets_the_failure_counter(
            self, bridge_mod, mock_logger, devices):
        h = self._attached(bridge_mod, mock_logger, devices)
        h.client.status = self._status(self.HEALTHY_CHURN)
        h.bridge.health_tick()
        h.client.fail["get_status"] = ConnectionError("blip")
        h.bridge.health_tick()                                     # failure 1
        del h.client.fail["get_status"]
        h.bridge.health_tick()                                     # success -> resets the counter
        h.client.fail["get_status"] = ConnectionError("blip again")
        h.bridge.health_tick()                                     # failure 1 again, not 2

        assert h.health_devices[0].states["subscriptionHealth"] == "healthy"


class TestHealthDeviceWriteFailureLatch:
    """§4.3 issue #286 review finding 3 — a failed WRITE (not a failed find or
    create) used to reach only DEBUG, and on the export-disabled path there is
    no next watchdog tick to retry it, so one failed write left "churning"
    standing for as long as export stayed off.
    """

    def _status(self, churn):
        return bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
            "subscriptionChurn": churn,
        })

    HEALTHY_CHURN = {"checked": True, "active": False, "peers": []}
    CHURNING_CHURN = {
        "checked": True, "active": True,
        "peers": [{"peerNodeId": "41869fbd537ef01", "fabricIndex": 2, "liveSessions": 5,
                    "invalidDeletions": 3, "windowMinutes": 30,
                    "since": "2026-08-23T09:12:00.000Z"}],
    }

    def _harness(self, bridge_mod, mock_logger, devices) -> Harness:
        return Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])

    def test_a_write_failure_is_retried_and_warned_exactly_once(
            self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)   # creates it, healthy
        dev = h.health_devices[0]
        dev.fail_writes = True

        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)  # write fails

        assert "could not update the bridge health device" in warnings_of(mock_logger)
        assert dev.states["subscriptionHealth"] == "healthy", "a failed write must not be believed"
        said = len(mock_logger.warning.call_args_list)

        dev.fail_writes = False
        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)  # retried; succeeds

        assert dev.states["subscriptionHealth"] == "churning"
        assert len(mock_logger.warning.call_args_list) == said, "success adds no new warning"

    def test_fail_succeed_fail_warns_twice(self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)
        dev = h.health_devices[0]

        dev.fail_writes = True
        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)  # fail 1
        dev.fail_writes = False
        h.bridge._on_attached(self._status(self.CHURNING_CHURN), False)  # succeed; clears latch
        dev.fail_writes = True
        h.bridge._on_attached(self._status(self.HEALTHY_CHURN), False)   # fail 2

        said = [call for call in mock_logger.warning.call_args_list
                if "could not update the bridge health device" in str(call.args[0])]
        assert len(said) == 2


class TestHealthDeviceFactory:
    """§4.3 issue #286 review finding 5 — the ONE production seam that finds
    or creates `matterBridgeHealth` was untested, and used a fixed name with
    none of `device_sync._create_one`'s collision handling: Indigo device
    names are server-global, so a stray existing "Matter Bridge Health"
    (including issue #62's `configured=False`, invisible-to-iter case) would
    make `indigo.device.create` raise `NameNotUnique` forever, and the
    swallowed exception's retry could never succeed on its own.
    """

    def test_find_only_returns_an_existing_device_without_creating(
            self, bridge_mod, mock_logger, mock_indigo_base):
        existing = RelayDevice(9001, bridge_mod.HEALTH_DEVICE_NAME,
                               deviceTypeId=bridge_mod.HEALTH_DEVICE_TYPE_ID)
        mock_indigo_base.devices = FakeIndigoDevices([existing])
        found = bridge_mod._find_health_device_only(mock_logger)
        assert found is existing
        assert not mock_indigo_base.device.create.called

    def test_find_only_returns_none_when_absent(
            self, bridge_mod, mock_logger, mock_indigo_base):
        mock_indigo_base.devices = FakeIndigoDevices([])
        assert bridge_mod._find_health_device_only(mock_logger) is None

    def test_find_only_ignores_a_device_of_a_different_type(
            self, bridge_mod, mock_logger, mock_indigo_base):
        other = RelayDevice(9002, "Something Else", deviceTypeId="matterRelay")
        mock_indigo_base.devices = FakeIndigoDevices([other])
        assert bridge_mod._find_health_device_only(mock_logger) is None

    def test_create_uses_the_plain_name_when_unused(
            self, bridge_mod, mock_logger, mock_indigo_base):
        mock_indigo_base.devices = FakeIndigoDevices([])
        created = object()
        mock_indigo_base.device.create.return_value = created

        result = bridge_mod._create_health_device(mock_logger)

        assert result is created
        _, kwargs = mock_indigo_base.device.create.call_args
        assert kwargs["name"] == bridge_mod.HEALTH_DEVICE_NAME
        assert kwargs["deviceTypeId"] == bridge_mod.HEALTH_DEVICE_TYPE_ID

    def test_create_resolves_a_name_collision_with_a_numeric_suffix(
            self, bridge_mod, mock_logger, mock_indigo_base):
        """Issue #62: a device already named "Matter Bridge Health" — of ANY
        type, including one a user re-typed away from matterBridgeHealth and
        is therefore invisible to `iter("self")` — must not make `create`
        raise forever."""
        collision = RelayDevice(555, bridge_mod.HEALTH_DEVICE_NAME)
        mock_indigo_base.devices = FakeIndigoDevices([collision])
        mock_indigo_base.device.create.return_value = object()

        bridge_mod._create_health_device(mock_logger)

        _, kwargs = mock_indigo_base.device.create.call_args
        assert kwargs["name"] == f"{bridge_mod.HEALTH_DEVICE_NAME} 2"

    def test_create_skips_a_taken_suffix_too(
            self, bridge_mod, mock_logger, mock_indigo_base):
        taken = [RelayDevice(555, bridge_mod.HEALTH_DEVICE_NAME),
                 RelayDevice(556, f"{bridge_mod.HEALTH_DEVICE_NAME} 2")]
        mock_indigo_base.devices = FakeIndigoDevices(taken)
        mock_indigo_base.device.create.return_value = object()

        bridge_mod._create_health_device(mock_logger)

        _, kwargs = mock_indigo_base.device.create.call_args
        assert kwargs["name"] == f"{bridge_mod.HEALTH_DEVICE_NAME} 3"

    def test_a_factory_returning_none_is_latch_logged(
            self, bridge_mod, mock_logger, devices):
        """A factory that returns `None` without raising used to be fully
        silent — same one-per-streak latch as the exception path."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.health_device_return_none = True

        h.bridge._on_attached(bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
            "subscriptionChurn": {"checked": True, "active": False, "peers": []},
        }), False)

        assert "could not create the bridge health device" in warnings_of(mock_logger)
        assert h.health_devices == []
        said = len(mock_logger.warning.call_args_list)

        h.bridge._on_attached(bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 1,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": [],
            "subscriptionChurn": {"checked": True, "active": True, "peers": [
                {"peerNodeId": "x", "fabricIndex": 1, "liveSessions": 5,
                 "invalidDeletions": 3, "windowMinutes": 30, "since": "2026-08-23T09:12:00.000Z"}]},
        }), False)
        assert len(mock_logger.warning.call_args_list) == said, "a standing failure is said once"


class TestRefusalRemedies:
    """⊗ One §1.1 error code, two OPPOSITE remedies.

    `endpoint_map_invalid` covers every refuse-to-start reason — §1.1 defines
    the state, not the cause — so the reason text is the only thing on the wire
    that says which fix applies. An unreadable MAP is fixed by §3.11's rebuild.
    An unreadable IDENTITY is not, and cannot be: the node refuses that rebuild
    outright, because clearing the refusal would leave the bridge serving under
    a `SerialNumber` no paired ecosystem has ever seen. Both used to get the map
    wording, which sent that user at the one door deliberately locked against
    them — and promised duplicated accessories on the way through it.
    """

    def test_an_unreadable_MAP_is_told_about_the_rebuild(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID,
                                    "endpoint map is unreadable; only get_status...")
        said = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                        else str(call.args[0]) for call in mock_logger.error.call_args_list)
        assert "endpoint-number map is unreadable" in said
        assert "rebuilt" in said

    def test_an_unreadable_IDENTITY_is_told_the_rebuild_will_NOT_help(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge._on_attach_refused(
            bridge_protocol.ERR_ENDPOINT_MAP_INVALID,
            f"{bridge_protocol.REFUSE_IDENTITY_UNREADABLE}; only get_status...")
        said = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                        else str(call.args[0]) for call in mock_logger.error.call_args_list)
        assert "identity file is unreadable" in said
        assert "will NOT fix this" in said
        assert "identity.json.unreadable-" in said
        assert "renumbers nothing" not in said, "the map remedy leaked into the identity case"


class TestDriftLatch:
    """`_on_drift_detected` had no latch, and drift is never repaired.

    After a `factory_reset preserveEndpointNumbers: true` that is EVERY exported
    device on every reconcile, with no way for the user to clear it short of
    §3.11 — so the one error that names the problem is buried under its own
    repetitions.
    """

    def _drift(self, *pairs):
        return bridge_protocol.parse_drift(
            [{"uniqueId": u, "expected": e, "actual": a} for u, e, a in pairs])

    def test_the_same_drift_set_is_reported_once(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        drift = self._drift(("indigo-1", 2, 5))
        h.bridge._on_drift_detected(drift)
        h.bridge._on_drift_detected(self._drift(("indigo-1", 2, 5)))
        assert mock_logger.error.call_count == 1

    def test_a_device_joining_the_drift_is_reported(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge._on_drift_detected(self._drift(("indigo-1", 2, 5)))
        h.bridge._on_drift_detected(self._drift(("indigo-1", 2, 5), ("indigo-2", 3, 6)))
        assert mock_logger.error.call_count == 2


class TestUnExportSnapshotAndGating:
    def test_the_pushed_snapshots_are_dropped_by_an_un_export(
            self, bridge_mod, mock_logger, devices):
        """Nothing is exported, so nothing has a last-pushed state.

        Left behind, the snapshots are a baseline for devices the node no longer
        holds — and the next diff against one would suppress a key the ecosystem
        has never actually been told.
        """
        devices.add(RelayDevice(101, "Lamp", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        assert h.bridge._pushed

        h.store.remove(101)
        h.bridge.exports_changed()

        assert h.bridge._pushed == {}

    def test_a_failed_submit_does_not_gate_start_forever(
            self, bridge_mod, mock_logger, devices):
        """`_un_exporting` gates `start()`, and its coroutine's `finally` is the
        only thing that clears it. If the coroutine was never scheduled, that
        gate stays shut for the life of the plugin and a user who re-adds a
        device gets a permanently inert bridge."""
        devices.add(RelayDevice(101, "Lamp", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.runtime.is_running = False

        h.store.remove(101)
        h.bridge.exports_changed()

        assert h.bridge._un_exporting is False
        h.runtime.is_running = True
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.bridge.active is True


class TestIndigoDeviceLookup:
    def test_a_missing_device_is_quiet(self, bridge_mod, mock_logger, mock_indigo_base):
        mock_indigo_base.devices = {}
        assert bridge_mod._indigo_device(999, mock_logger) is None
        assert "could not read Indigo device" not in warnings_of(mock_logger)

    def test_indigo_failing_to_answer_is_NOT_reported_as_a_deleted_device(
            self, bridge_mod, mock_logger, mock_indigo_base):
        """The bare `except Exception` gave both the same silent None.

        "Device %s no longer exists" sends the user to delete an export that was
        never the problem; a broken IPC connection needs the opposite response.
        """
        class _Broken:
            def __getitem__(self, _key):
                raise ConnectionError("IndigoServer went away")

        mock_indigo_base.devices = _Broken()
        assert bridge_mod._indigo_device(101, mock_logger) is None
        said = warnings_of(mock_logger)
        assert "could not read Indigo device" in said
        assert "NOT the device having been deleted" in said
