"""E3: the per-role OUTBOUND handler table (`export_handlers`).

Two layers, deliberately:

* a **zoo table test** over every implemented role, asserting the invariants
  that make PRD §XG7 true — every E3 role has a handler, every state key it
  emits is in that role's §4.2 vocabulary, and every §4.2 command that role
  declares is dispatchable. Adding a role without its vocabularies fails here,
  which is the point;
* per-role behaviour: what ``states_for`` reads off a real-shaped Indigo device,
  what ``diff`` decides has changed, and which ``indigo.*`` call ``dispatch``
  actually makes.

References to ``§N`` are ``docs/BRIDGE_PROTOCOL.md``.
"""
from __future__ import annotations

import importlib

import pytest

import bridge_protocol
from fakes import DimmerDevice, RelayDevice


@pytest.fixture
def handlers(mock_indigo_base):
    """`export_handlers` bound to a mocked ``indigo`` module.

    The module imports ``indigo`` at top level (it has to — it calls
    ``indigo.device.turnOn``), so it must be reloaded after the mock is in
    ``sys.modules``. Reload mutates the module object in place, so anything
    already holding a reference to it sees the rebound ``indigo`` too.
    """
    import export_handlers as module
    importlib.reload(module)
    return module


# ---------------------------------------------------------------------------
# The zoo (invariants over the whole table)
# ---------------------------------------------------------------------------
E3_ROLES = (
    "onOffPlugInUnit", "onOffLight", "dimmableLight",
    "colorTemperatureLight", "extendedColorLight",
)


def test_every_e3_role_has_a_handler(handlers):
    assert set(handlers.HANDLERS) == set(E3_ROLES)


def test_no_handler_claims_a_role_outside_the_protocol_enum(handlers):
    assert set(handlers.HANDLERS) <= bridge_protocol.ROLES


@pytest.mark.parametrize("role", E3_ROLES)
def test_the_handler_is_registered_under_its_own_role(handlers, role):
    assert handlers.HANDLERS[role].role == role


@pytest.mark.parametrize("role", E3_ROLES)
def test_state_keys_are_a_subset_of_the_roles_4_2_vocabulary(handlers, role):
    """A key the node does not know for this role is dropped on the floor."""
    dev = _fully_populated_device()
    produced = set(handlers.HANDLERS[role].states_for(dev))
    allowed = set(bridge_protocol.ROLE_STATE_KEYS[role])
    assert produced <= allowed, f"{role} emits {produced - allowed}"


@pytest.mark.parametrize("role", E3_ROLES)
def test_a_capable_device_produces_the_whole_vocabulary(handlers, role):
    """The other direction: a device that CAN answer every key must answer it.

    Subset-only would pass for a handler that silently stopped reporting
    brightness, which an ecosystem renders as a light stuck at its last level.
    """
    dev = _fully_populated_device()
    assert set(handlers.HANDLERS[role].states_for(dev)) == \
        set(bridge_protocol.ROLE_STATE_KEYS[role])


@pytest.mark.parametrize("role", E3_ROLES)
def test_dispatch_covers_every_command_the_role_declares(handlers, role):
    assert set(handlers.HANDLERS[role].commands()) == set(bridge_protocol.ROLE_COMMANDS[role])


@pytest.mark.parametrize("role", sorted(bridge_protocol.ROLES - set(E3_ROLES)))
def test_an_e4_role_is_explicitly_unbridgeable(handlers, role):
    """E4 roles must answer ``None``, not raise and not half-work.

    The allow-list can already hold them — the §5.1 dialog offers doorLock,
    windowCovering and the sensors as roles — and an unknown role fails the
    WHOLE attach on the node (E3a), so the caller has to be able to ask.
    """
    assert handlers.handler_for(role) is None
    assert not handlers.is_bridgeable(role)


def _fully_populated_device():
    """A dimmer that can answer every E3 state key."""
    return DimmerDevice(1, "Zoo Lamp", onState=True, brightness=60,
                        redLevel=100, greenLevel=0, blueLevel=0,
                        whiteLevel=80, whiteTemperature=2700,
                        supportsRGB=True, supportsWhiteTemperature=True)


# ---------------------------------------------------------------------------
# onOff (plug + light)
# ---------------------------------------------------------------------------
class TestOnOff:
    def test_states_read_on_state(self, handlers):
        handler = handlers.handler_for("onOffPlugInUnit")
        assert handler.states_for(RelayDevice(1, "Plug", onState=True)) == {"onOff": True}
        assert handler.states_for(RelayDevice(1, "Plug", onState=False)) == {"onOff": False}

    def test_diff_is_empty_when_nothing_moved(self, handlers):
        handler = handlers.handler_for("onOffLight")
        before = RelayDevice(1, "Lamp", onState=True)
        after = RelayDevice(1, "Lamp", onState=True)
        assert handler.diff(before, after) == {}

    def test_diff_reports_the_flip(self, handlers):
        handler = handlers.handler_for("onOffLight")
        assert handler.diff(RelayDevice(1, "L", onState=False),
                            RelayDevice(1, "L", onState=True)) == {"onOff": True}

    def test_dispatch_true_turns_on(self, handlers, mock_indigo_base):
        dev = RelayDevice(1, "Plug")
        assert handlers.handler_for("onOffPlugInUnit").dispatch("onOff", {"value": True}, dev)
        mock_indigo_base.device.turnOn.assert_called_once_with(dev)
        mock_indigo_base.device.turnOff.assert_not_called()

    def test_dispatch_false_turns_off(self, handlers, mock_indigo_base):
        dev = RelayDevice(1, "Plug")
        assert handlers.handler_for("onOffPlugInUnit").dispatch("onOff", {"value": False}, dev)
        mock_indigo_base.device.turnOff.assert_called_once_with(dev)
        mock_indigo_base.device.turnOn.assert_not_called()

    def test_an_unknown_command_is_refused_not_guessed(self, handlers, mock_indigo_base):
        dev = RelayDevice(1, "Plug")
        assert handlers.handler_for("onOffPlugInUnit").dispatch("setLevel", {"level": 5}, dev) \
            is False
        mock_indigo_base.device.turnOn.assert_not_called()


# ---------------------------------------------------------------------------
# dimmableLight
# ---------------------------------------------------------------------------
class TestDimmable:
    def test_states_carry_on_off_and_level(self, handlers):
        dev = DimmerDevice(1, "Lamp", onState=True, brightness=42)
        assert handlers.handler_for("dimmableLight").states_for(dev) == \
            {"onOff": True, "level": 42}

    def test_level_is_omitted_when_the_device_cannot_answer(self, handlers):
        """A relay exported as a dimmable light has no ``brightness`` at all."""
        dev = RelayDevice(1, "Plug", onState=True)
        assert handlers.handler_for("dimmableLight").states_for(dev) == {"onOff": True}

    def test_diff_reports_only_the_changed_key(self, handlers):
        handler = handlers.handler_for("dimmableLight")
        before = DimmerDevice(1, "Lamp", onState=True, brightness=40)
        after = DimmerDevice(1, "Lamp", onState=True, brightness=75)
        assert handler.diff(before, after) == {"level": 75}

    def test_dispatch_sets_brightness_on_the_same_0_100_scale(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Lamp")
        assert handlers.handler_for("dimmableLight").dispatch("setLevel", {"level": 60}, dev)
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(dev, value=60)

    def test_dispatch_clamps_an_out_of_range_level(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Lamp")
        handlers.handler_for("dimmableLight").dispatch("setLevel", {"level": 140}, dev)
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(dev, value=100)

    def test_dispatch_without_a_level_raises_rather_than_guessing(self, handlers):
        with pytest.raises(ValueError):
            handlers.handler_for("dimmableLight").dispatch("setLevel", {}, DimmerDevice(1, "L"))


# ---------------------------------------------------------------------------
# colorTemperatureLight
# ---------------------------------------------------------------------------
class TestColorTemperature:
    def test_kelvin_becomes_mireds(self, handlers):
        dev = DimmerDevice(1, "Lamp", onState=True, brightness=50, whiteTemperature=2700)
        states = handlers.handler_for("colorTemperatureLight").states_for(dev)
        assert states["colorTempMireds"] == 370        # 1e6 / 2700

    def test_mireds_stay_inside_the_declared_domain(self, handlers):
        """§4.2 declares 153-500. A 10000K device converts to 100."""
        dev = DimmerDevice(1, "Lamp", onState=True, brightness=50, whiteTemperature=10000)
        states = handlers.handler_for("colorTemperatureLight").states_for(dev)
        assert states["colorTempMireds"] == 153

    def test_an_unset_temperature_is_omitted_not_zeroed(self, handlers):
        for value in (None, 0):
            dev = DimmerDevice(1, "Lamp", onState=True, brightness=50, whiteTemperature=value)
            assert "colorTempMireds" not in \
                handlers.handler_for("colorTemperatureLight").states_for(dev)

    def test_dispatch_preserves_the_white_level(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Lamp", whiteLevel=40)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 370}, dev)
        mock_indigo_base.dimmer.setColorLevels.assert_called_once_with(
            dev, whiteLevel=40, whiteTemperature=2703)

    def test_dispatch_defaults_a_missing_white_level_to_full(self, handlers, mock_indigo_base):
        """A colour tweak must never be the thing that blacks out the room."""
        dev = DimmerDevice(1, "Lamp", whiteLevel=None)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 250}, dev)
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteLevel"] == 100


# ---------------------------------------------------------------------------
# extendedColorLight
# ---------------------------------------------------------------------------
class TestExtendedColor:
    def test_rgb_becomes_hue_and_saturation(self, handlers):
        dev = DimmerDevice(1, "Lamp", onState=True, brightness=50,
                           redLevel=100, greenLevel=0, blueLevel=0)
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert states["hue"] == 0 and states["saturation"] == 100
        dev.redLevel, dev.greenLevel, dev.blueLevel = 0, 0, 100
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert states["hue"] == 240 and states["saturation"] == 100

    def test_colour_keys_are_omitted_when_a_channel_is_unset(self, handlers):
        dev = DimmerDevice(1, "Lamp", onState=True, brightness=50,
                           redLevel=100, greenLevel=0, blueLevel=None)
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert "hue" not in states and "saturation" not in states

    def test_a_one_degree_hue_wobble_is_not_a_change(self, handlers):
        """E3a: hue round-trips through Matter's 0-254 scale ±1°.

        Without the tolerance every ecosystem-driven colour change echoes a
        spurious ``set_state`` straight back out.
        """
        handler = handlers.handler_for("extendedColorLight")
        before = DimmerDevice(1, "L", onState=True, brightness=50,
                              redLevel=100, greenLevel=2, blueLevel=0)
        after = DimmerDevice(1, "L", onState=True, brightness=50,
                             redLevel=100, greenLevel=4, blueLevel=0)
        assert handler.states_for(before)["hue"] == 1
        assert handler.states_for(after)["hue"] == 2
        assert handler.states_for(before)["saturation"] == \
            handler.states_for(after)["saturation"], "only hue may move in this fixture"
        assert handler.diff(before, after) == {}

    def test_a_real_hue_move_is_still_reported(self, handlers):
        handler = handlers.handler_for("extendedColorLight")
        before = DimmerDevice(1, "L", onState=True, brightness=50,
                              redLevel=100, greenLevel=0, blueLevel=0)
        after = DimmerDevice(1, "L", onState=True, brightness=50,
                             redLevel=0, greenLevel=0, blueLevel=100)
        assert handler.diff(before, after)["hue"] == 240

    def test_saturation_has_no_tolerance(self, handlers):
        """0-100 → 0-254 → 0-100 round-trips exactly, so ±1 IS a real change."""
        handler = handlers.handler_for("extendedColorLight")
        before = DimmerDevice(1, "L", onState=True, brightness=50,
                              redLevel=100, greenLevel=0, blueLevel=0)
        after = DimmerDevice(1, "L", onState=True, brightness=50,
                             redLevel=100, greenLevel=1, blueLevel=1)
        assert handler.diff(before, after) == {"saturation": 99}

    def test_dispatch_writes_rgb_levels(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Lamp")
        handlers.handler_for("extendedColorLight").dispatch(
            "setColor", {"hue": 240, "saturation": 100}, dev)
        mock_indigo_base.dimmer.setColorLevels.assert_called_once_with(
            dev, redLevel=0, greenLevel=0, blueLevel=100)

    def test_the_duplicate_set_color_is_dispatched_again_not_swallowed(
            self, handlers, mock_indigo_base):
        """E3a: ecosystems send ``setColor`` twice with identical pairs.

        ``setColorLevels`` takes absolute values, so the repeat is a no-op at the
        lamp — and de-duplicating on "we already believe that" would skip the
        FIRST call too whenever Indigo's belief has drifted from the hardware.
        """
        dev = DimmerDevice(1, "Lamp")
        args = {"hue": 120, "saturation": 50}
        handler = handlers.handler_for("extendedColorLight")
        handler.dispatch("setColor", dict(args), dev)
        handler.dispatch("setColor", dict(args), dev)
        assert mock_indigo_base.dimmer.setColorLevels.call_count == 2
        first, second = mock_indigo_base.dimmer.setColorLevels.call_args_list
        assert first == second

    def test_dispatch_without_a_numeric_pair_raises(self, handlers):
        handler = handlers.handler_for("extendedColorLight")
        with pytest.raises(ValueError):
            handler.dispatch("setColor", {"hue": 120}, DimmerDevice(1, "L"))


# ---------------------------------------------------------------------------
# Colour conversion (pure)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rgb,expected", [
    ((100, 0, 0), (0, 100)),
    ((0, 100, 0), (120, 100)),
    ((0, 0, 100), (240, 100)),
    ((100, 100, 100), (0, 0)),
    ((0, 0, 0), (0, 0)),
])
def test_rgb_to_hue_saturation(handlers, rgb, expected):
    assert handlers.rgb_to_hue_saturation(*rgb) == expected


@pytest.mark.parametrize("hue", [0, 45, 120, 200, 300, 359])
def test_hue_survives_a_round_trip_through_rgb(handlers, hue):
    """The conversion pair must not drift more than the declared tolerance."""
    red, green, blue = handlers.hue_saturation_to_rgb(hue, 100)
    back, _saturation = handlers.rgb_to_hue_saturation(red, green, blue)
    assert abs(back - hue) <= handlers.HUE_TOLERANCE_DEGREES
