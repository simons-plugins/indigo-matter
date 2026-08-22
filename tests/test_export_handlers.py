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
import fakes
from fakes import (
    DimmerDevice,
    FakeIndigoDevice,
    RelayDevice,
    SensorDevice,
    ThermostatDevice,
)


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
ALL_ROLES = tuple(sorted(bridge_protocol.ROLES))


def test_every_v1_role_has_a_handler(handlers):
    """The E4 completion criterion: the table and the §4.2 enum are one set.

    Not a subset either way. A missing handler is an export the user selected
    that never appears in any ecosystem; an extra one is a role the node would
    refuse with ``unknown_role``, failing the whole attach.
    """
    assert set(handlers.HANDLERS) == bridge_protocol.ROLES
    assert handlers.BRIDGEABLE_ROLES == bridge_protocol.ROLES


@pytest.mark.parametrize("role", ALL_ROLES)
def test_the_handler_is_registered_under_its_own_role(handlers, role):
    assert handlers.HANDLERS[role].role == role
    assert handlers.is_bridgeable(role)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_state_keys_are_a_subset_of_the_roles_4_2_vocabulary(handlers, role):
    """A key the node does not know for this role is dropped on the floor."""
    produced = set(handlers.HANDLERS[role].states_for(_capable_device(role)))
    allowed = set(bridge_protocol.ROLE_STATE_KEYS[role])
    assert produced <= allowed, f"{role} emits {produced - allowed}"


#: extendedColorLight is mode-aware (issue #282) and covered separately below
#: — no single snapshot carries its whole vocabulary by design.
_WHOLE_VOCABULARY_ROLES = tuple(role for role in ALL_ROLES if role != "extendedColorLight")


@pytest.mark.parametrize("role", _WHOLE_VOCABULARY_ROLES)
def test_a_capable_device_produces_the_whole_vocabulary(handlers, role):
    """The other direction: a device that CAN answer every key must answer it.

    Subset-only would pass for a handler that silently stopped reporting
    brightness, which an ecosystem renders as a light stuck at its last level.
    """
    assert set(handlers.HANDLERS[role].states_for(_capable_device(role))) == \
        set(bridge_protocol.ROLE_STATE_KEYS[role])


def test_extended_color_light_reaches_its_whole_vocabulary_across_both_modes(handlers):
    """extendedColorLight's version of the invariant above (issue #282).

    ``states_for`` now emits exactly ONE colour channel per snapshot —
    ``colorTempMireds`` XOR ``hue``/``saturation`` — chosen by the device's
    colour mode, so no single capable device produces every §4.2 key this
    role owns; a device that is simultaneously in two colour modes does not
    exist. The vocabulary is still fully reachable, just across modes rather
    than within one snapshot: a CT-mode device's snapshot and a hue/sat-mode
    device's snapshot together cover it, and the only overlap is the
    mode-independent ``onOff``/``level`` pair.
    """
    handler = handlers.handler_for("extendedColorLight")
    ct_device = DimmerDevice(1, "CT Lamp", onState=True, brightness=60,
                             redLevel=0, greenLevel=0, blueLevel=0,
                             whiteTemperature=2700, supportsRGB=True,
                             supportsWhiteTemperature=True)
    hue_device = _fully_populated_device()
    ct_states = set(handler.states_for(ct_device))
    hue_states = set(handler.states_for(hue_device))
    assert ct_states == {"onOff", "level", "colorTempMireds"}
    assert hue_states == {"onOff", "level", "hue", "saturation"}
    assert ct_states | hue_states == set(bridge_protocol.ROLE_STATE_KEYS["extendedColorLight"])


#: The roles E4 added. Kept as a list because several invariants below are true
#: of them and NOT of the on/off family — see the fabrication test.
E4_ROLES = ("windowCovering", "doorLock", "occupancySensor", "contactSensor",
            "temperatureSensor", "humiditySensor", "lightSensor",
            "pressureSensor", "flowSensor", "thermostat")


@pytest.mark.parametrize("role", E4_ROLES)
def test_a_device_that_answers_nothing_produces_no_fabricated_keys(handlers, role):
    """§3.4 state maps are partial; a fabricated value goes out as fact.

    The dangerous ones are the booleans — a lock or a contact sensor defaulting
    to False tells every ecosystem the door is shut, which is the reading a user
    would act on. Every E4 attribute is documented nullable in the IOM
    (``onState``/``sensorValue`` are "None if the sensor doesn't support…"), so
    "say nothing" is always available and always the honest answer.

    The on/off light family is deliberately excluded: a relay always has an
    ``onState``, so ``onOff`` is unconditional there and has been since E3.
    """
    silent = FakeIndigoDevice(1, "Mute", onState=None, sensorValue=None,
                              brightness=None, temperatures=None,
                              heatSetpoint=None, coolSetpoint=None, hvacMode=None)
    assert handlers.HANDLERS[role].states_for(silent) == {}


@pytest.mark.parametrize("role", ALL_ROLES)
def test_dispatch_covers_every_command_the_role_declares(handlers, role):
    assert set(handlers.HANDLERS[role].commands()) == set(bridge_protocol.ROLE_COMMANDS[role])


@pytest.mark.parametrize("role", ALL_ROLES)
def test_a_diff_of_a_device_against_itself_is_empty(handlers, role):
    """Every role, not just the ones with an interesting tolerance.

    A handler that recomputed a value non-deterministically — or compared an
    inverted reading against a raw one — would push a `set_state` on every
    unrelated device change in the house.
    """
    dev = _capable_device(role)
    assert handlers.HANDLERS[role].diff(dev, _capable_device(role)) == {}


def test_the_sensor_roles_declare_no_commands(handlers):
    """Matter sensors are read-only; an empty tuple in §4.2 is by design."""
    for role in ("occupancySensor", "contactSensor", "temperatureSensor",
                 "humiditySensor", "lightSensor", "pressureSensor", "flowSensor"):
        assert handlers.HANDLERS[role].commands() == {}


def test_the_invert_option_key_matches_the_stores(handlers):
    """The two spell it separately so neither imports the other; pin them here."""
    import export_store
    assert handlers.OPTION_INVERT == export_store.OPTION_INVERT
    assert export_store.INVERTIBLE_ROLES == ("windowCovering",)


def _capable_device(role: str):
    """A device shaped so that ``role``'s handler can answer every §4.2 key.

    One Frankenstein device per role rather than one for all fifteen: a single
    object carrying ``sensorValue``, ``temperatures`` and RGB channels at once
    exists nowhere in Indigo, and the invariant it would prove ("the handler
    reads *something*") is weaker than the one here ("the handler reads what a
    real device of this kind actually offers").
    """
    if role in ("occupancySensor", "contactSensor"):
        return SensorDevice(1, "Zoo Sensor", onState=True, supportsOnState=True)
    if role in ("temperatureSensor", "humiditySensor", "lightSensor",
                "pressureSensor", "flowSensor"):
        return SensorDevice(1, "Zoo Sensor", sensorValue=21.5, supportsSensorValue=True)
    if role == "thermostat":
        # `indigo` is the conftest mock here, so kHvacMode.Heat is a stable
        # sentinel object — which is exactly how the real enum members compare.
        import indigo
        return ThermostatDevice(1, "Zoo Stat", temperatures=[20.5], heatSetpoint=21.0,
                                coolSetpoint=24.0, hvacMode=indigo.kHvacMode.Heat)
    if role == "doorLock":
        return RelayDevice(1, "Zoo Lock", onState=True)
    if role == "windowCovering":
        return DimmerDevice(1, "Zoo Blind", onState=True, brightness=70)
    return _fully_populated_device()


def _fully_populated_device():
    """A dimmer that can answer every light role's state key."""
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

    def test_dispatch_of_level_zero_is_an_off_not_a_missing_level(self, handlers,
                                                                  mock_indigo_base):
        """T3: 0 is falsy, and the guard has to be a type check, not a truth test."""
        dev = DimmerDevice(1, "Lamp")
        assert handlers.handler_for("dimmableLight").dispatch("setLevel", {"level": 0}, dev)
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(dev, value=0)

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

    def test_a_1800k_lamp_is_clamped_to_the_declared_maximum(self, handlers):
        """T3: the other end of the domain. 1800K → 556 mireds → clamped to 500.

        The MIREDS_MIN side has a test above; without this one the ``_clamp``
        call could lose its upper bound and nothing would notice until a warm
        lamp pushed an out-of-domain value the node then had to clamp for us.
        """
        dev = DimmerDevice(1, "Lamp", onState=True, brightness=50, whiteTemperature=1800)
        states = handlers.handler_for("colorTemperatureLight").states_for(dev)
        assert states["colorTempMireds"] == handlers.MIREDS_MAX == 500

    def test_a_device_with_no_white_channel_is_skipped_not_guessed_at(
            self, handlers, mock_indigo_base):
        """X4: ``whiteLevel is None`` means there is no white channel at all.

        Inventing ``whiteLevel=100`` for such a device asks its driver to drive a
        channel it does not have — and the previous "default to full" rule was
        written for a device that HAS the channel and reports it off.
        """
        dev = DimmerDevice(1, "Lamp", whiteLevel=None)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 250}, dev)
        mock_indigo_base.dimmer.setColorLevels.assert_not_called()

    def test_the_skip_is_REPORTED_as_a_no_op_rather_than_as_a_success(
            self, handlers, mock_indigo_base):
        """The tri-state, or the caller latches nothing and says nothing.

        ``dispatch`` ends in ``handler(...) or True``, so a bare ``None`` from
        the skip above reported SUCCESS for a command that did nothing at all —
        the ecosystem had already flipped its tile to the new colour
        temperature, the lamp never moved, and the only trace was a debug line
        from a static method that cannot name the device.
        """
        dev = DimmerDevice(1, "Lamp", whiteLevel=None)
        outcome = handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 250}, dev)
        assert isinstance(outcome, str), f"expected a no-op reason, got {outcome!r}"
        assert "no white channel" in outcome

    def test_a_real_write_still_reports_plain_success(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Lamp", whiteLevel=50)
        assert handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 250}, dev) is True

    def test_a_white_channel_reporting_off_keeps_its_real_level(
            self, handlers, mock_indigo_base):
        """E5: `whiteLevel or 100` caught the real 0 — `0.0` is falsy.

        The two zero-ish answers are different facts and need opposite
        treatment, which is what the truthiness test destroyed. ``None`` means
        "no white channel exists" and is handled above by skipping the write
        entirely; **0** means "the channel exists and is currently off", and it
        is the device's real state. Defaulting it to 100 turned a colour-
        temperature change into a colour-temperature change *plus* switching the
        white channel to full — a bridge turning a lamp on that nobody asked it
        to, in response to a command Matter defines as orthogonal to on/off.
        """
        dev = DimmerDevice(1, "Lamp", whiteLevel=0)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 250}, dev)
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteLevel"] == 0
        # The thing the command WAS for still happens.
        assert kwargs["whiteTemperature"] == 4000

    def test_a_lit_white_channel_keeps_its_level_too(self, handlers, mock_indigo_base):
        """The general rule the 0 case is an instance of: preserve, never invent."""
        dev = DimmerDevice(1, "Lamp", whiteLevel=35)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 250}, dev)
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteLevel"] == 35

    @pytest.mark.parametrize("mireds,expected_kelvin", [
        (1, 6536),        # → clamped up to MIREDS_MIN 153
        (10000, 2000),    # → clamped down to MIREDS_MAX 500
    ])
    def test_out_of_domain_mireds_are_clamped_before_the_indigo_write(
            self, handlers, mock_indigo_base, mireds, expected_kelvin):
        """X2: the node clamps too — but our Indigo write must not depend on it.

        Unclamped, 1 mired writes 1,000,000 K and 10000 mireds writes 100 K.
        Indigo's own whiteTemperature domain is 1200–15000, so both are values
        the driver has to reject or silently mangle, in the OTHER process.
        """
        dev = DimmerDevice(1, "Lamp", whiteLevel=40)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": mireds}, dev)
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteTemperature"] == expected_kelvin
        assert 1200 <= kwargs["whiteTemperature"] <= 15000

    def test_fractional_mireds_round_rather_than_truncate(self, handlers, mock_indigo_base):
        """X2: ``int()`` on 369.9 is 369, which is a different colour."""
        dev = DimmerDevice(1, "Lamp", whiteLevel=40)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 369.9}, dev)
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteTemperature"] == 2703          # 1e6 / 370, not 1e6 / 369

    def test_setting_a_temperature_leaves_the_rgb_channels_alone(self, handlers, mock_indigo_base):
        """Issue #282: this used to also zero redLevel/greenLevel/blueLevel.

        That zero was a real ``setColorLevels`` *hardware* command — a
        channel-publishing driver (z2m) puts ``{"color":{"r":0,"g":0,"b":0}}``
        on the wire, so an RGBW lamp flashed deep blue on every colour-
        temperature write. Coherence for the node's colour-mode belief now
        lives on the reporting side (``ExtendedColorLightExport.states_for``
        publishes one channel per snapshot), so this write touches only the
        white channel — exactly the keys, no companions.
        """
        dev = DimmerDevice(1, "Lamp", whiteLevel=40, redLevel=100, greenLevel=0, blueLevel=0)
        handlers.handler_for("colorTemperatureLight").dispatch(
            "setColorTemp", {"colorTempMireds": 370}, dev)
        mock_indigo_base.dimmer.setColorLevels.assert_called_once_with(
            dev, whiteLevel=40, whiteTemperature=2703)


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

    def test_hue_is_omitted_below_the_saturation_floor(self, handlers):
        """X3: at sat 5 the integer-RGB round trip moves hue by up to 6°.

        The ±1° tolerance cannot absorb that, so every pastel nudge pushed a
        ``hue`` the ecosystem had not asked for and the Home wheel jumped.
        """
        handler = handlers.handler_for("extendedColorLight")
        dev = DimmerDevice(1, "L", onState=True, brightness=50,
                           redLevel=100, greenLevel=95, blueLevel=95)
        states = handler.states_for(dev)
        assert states["saturation"] == 5
        assert "hue" not in states

    def test_a_pastel_hue_wobble_pushes_nothing(self, handlers):
        handler = handlers.handler_for("extendedColorLight")
        before = DimmerDevice(1, "L", onState=True, brightness=50,
                              redLevel=100, greenLevel=95, blueLevel=95)      # hue 0
        after = DimmerDevice(1, "L", onState=True, brightness=50,
                             redLevel=100, greenLevel=95, blueLevel=100)      # hue 300
        assert handler.states_for(before)["saturation"] == \
            handler.states_for(after)["saturation"] == 5, "only hue may move here"
        assert handler.diff(before, after) == {}

    def test_a_fully_desaturated_device_reports_no_hue(self, handlers):
        """At sat 0 hue is not merely noisy, it is undefined — RGB says 0 always."""
        handler = handlers.handler_for("extendedColorLight")
        dev = DimmerDevice(1, "L", onState=True, brightness=50,
                           redLevel=100, greenLevel=100, blueLevel=100)
        assert handler.states_for(dev) == {"onOff": True, "level": 50, "saturation": 0}

    def test_at_the_floor_itself_hue_is_reported_again(self, handlers):
        """sat 20 is where the measured error falls to the ±1° tolerance."""
        handler = handlers.handler_for("extendedColorLight")
        dev = DimmerDevice(1, "L", onState=True, brightness=50,
                           redLevel=100, greenLevel=80, blueLevel=80)
        states = handler.states_for(dev)
        assert states["saturation"] == handlers.SATURATION_HUE_FLOOR == 20
        assert states["hue"] == 0

    def test_a_saturated_hue_move_is_unaffected_by_the_floor(self, handlers):
        handler = handlers.handler_for("extendedColorLight")
        before = DimmerDevice(1, "L", onState=True, brightness=50,
                              redLevel=100, greenLevel=80, blueLevel=80)      # sat 20, hue 0
        after = DimmerDevice(1, "L", onState=True, brightness=50,
                             redLevel=100, greenLevel=80, blueLevel=100)      # sat 20, hue 300
        assert handler.diff(before, after) == {"hue": 300}

    def test_setting_a_colour_leaves_the_white_channel_alone(self, handlers, mock_indigo_base):
        """Issue #282: the mirror of the CT test above — this used to also
        zero whiteLevel, a hardware write that put ``{"brightness":0}`` on
        the wire for a channel-publishing driver, switching the lamp off the
        instant a user picked a colour. Only the RGB keys are written now."""
        dev = DimmerDevice(1, "Lamp", whiteLevel=80)
        handlers.handler_for("extendedColorLight").dispatch(
            "setColor", {"hue": 240, "saturation": 100}, dev)
        mock_indigo_base.dimmer.setColorLevels.assert_called_once_with(
            dev, redLevel=0, greenLevel=0, blueLevel=100)

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


class TestExtendedColorModeAwareness:
    """Issue #282: ``states_for`` publishes ONE colour channel, chosen by mode.

    A device holding both live RGB levels AND a white temperature (an RGBW
    bulb between colour writes) used to have both channels emitted together —
    that is exactly what let the write side believe it had to zero one of
    them to keep the node coherent. These pin the reporting-side replacement:
    the mode decides which single channel goes out, adversarially phrased as
    "does the OTHER channel ever leak through".
    """

    def _rgbw(self, **overrides):
        kwargs = dict(onState=True, brightness=50, redLevel=100, greenLevel=0,
                      blueLevel=0, whiteTemperature=2700, supportsRGB=True,
                      supportsWhiteTemperature=True)
        kwargs.update(overrides)
        return DimmerDevice(1, "Lamp", **kwargs)

    def test_color_temp_mode_carries_ct_not_hue_or_saturation(self, handlers):
        """z2m ``colorMode: "color_temp"`` selects the CT channel."""
        dev = self._rgbw(states={"colorMode": "color_temp"})
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert "colorTempMireds" in states
        assert "hue" not in states and "saturation" not in states

    @pytest.mark.parametrize("mode", ["xy", "hs"])
    def test_xy_or_hs_mode_carries_hue_saturation_not_ct(self, handlers, mode):
        """z2m reports either ``xy`` or ``hs`` for a colour write; both mean hue/sat."""
        dev = self._rgbw(states={"colorMode": mode})
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert "hue" in states and "saturation" in states
        assert "colorTempMireds" not in states

    def test_no_colormode_state_falls_back_to_ct_when_rgb_is_zero_and_white_is_set(
            self, handlers):
        """Non-z2m plugins never publish ``colorMode`` at all. RGB all zero plus
        a usable ``whiteTemperature`` reads as "this device is presenting CT"."""
        dev = self._rgbw(redLevel=0, states={})
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert "colorTempMireds" in states
        assert "hue" not in states and "saturation" not in states

    def test_no_colormode_state_falls_back_to_hue_saturation_when_rgb_is_live(
            self, handlers):
        dev = self._rgbw(states={})
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert "hue" in states and "saturation" in states
        assert "colorTempMireds" not in states

    def test_a_bogus_colormode_does_not_crash_and_falls_back_to_the_heuristic(
            self, handlers):
        """A driver using different vocabulary (or garbage) must still resolve
        to a single coherent channel rather than raising."""
        dev = self._rgbw(redLevel=0, states={"colorMode": "rainbow"})
        states = handlers.handler_for("extendedColorLight").states_for(dev)
        assert "colorTempMireds" in states
        assert "hue" not in states and "saturation" not in states

    def test_losing_all_colour_sources_reports_the_colour_keys_as_stopped(
            self, handlers):
        """The stopped-keys exemption (issue #282 trap (a)) must not swallow a
        GENUINE total loss of colour capability — the workspace's degradation-
        path convention forbids a silent swallow. The exemption only fires
        when the new snapshot still carries a mode-alternating key; a device
        whose driver has lost it entirely (no RGB, no whiteTemperature, no
        colorMode) has nothing in ``after`` for that test to key off, so
        ``hue``/``saturation`` must come back as ``stopped``, not vanish.
        """
        handler = handlers.handler_for("extendedColorLight")
        hue_dev = self._rgbw(redLevel=100, greenLevel=0, blueLevel=0,
                             whiteTemperature=None, states={"colorMode": "hs"})
        pushed = handler.states_for(hue_dev)
        assert "hue" in pushed and "saturation" in pushed

        dark_dev = self._rgbw(redLevel=None, greenLevel=None, blueLevel=None,
                              whiteTemperature=None, states={})
        diff = handler.diff_from(pushed, dark_dev)
        assert {"hue", "saturation"} <= diff.stopped

    def test_ct_mode_with_unusable_kelvin_reports_no_colour_channel_and_does_not_crash(
            self, handlers):
        """Pins the degradation branch: ``colorMode == "color_temp"`` but
        ``whiteTemperature`` is ``None``/``0`` (both real Indigo for "not
        telling us"). ``use_color_temp`` is still True, but ``kelvin`` is
        falsy, so ``states_for`` must publish neither colour channel — not
        crash, and not silently fall back to hue/sat behind the mode's back.
        The vanished channel must also be reported ``stopped`` by
        ``diff_from``, exactly like the total-loss case above: ``after`` here
        carries no mode-alternating key either.
        """
        handler = handlers.handler_for("extendedColorLight")
        hue_dev = self._rgbw(redLevel=100, greenLevel=0, blueLevel=0,
                             whiteTemperature=None, states={"colorMode": "hs"})
        pushed = handler.states_for(hue_dev)

        for unusable_kelvin in (None, 0):
            ct_dev = self._rgbw(redLevel=100, greenLevel=0, blueLevel=0,
                                whiteTemperature=unusable_kelvin,
                                states={"colorMode": "color_temp"})
            states = handler.states_for(ct_dev)
            assert "colorTempMireds" not in states
            assert "hue" not in states and "saturation" not in states

            diff = handler.diff_from(pushed, ct_dev)
            assert {"hue", "saturation"} <= diff.stopped


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


# ---------------------------------------------------------------------------
# windowCovering (E4)
# ---------------------------------------------------------------------------
class TestWindowCovering:
    """§4.2 ``position`` is 0-100 with 100 = open — the inbound convention."""

    @staticmethod
    def _handler(handlers):
        return handlers.HANDLERS["windowCovering"]

    def test_position_is_the_brightness_when_polarity_is_normal(self, handlers):
        dev = DimmerDevice(1, "Blind", brightness=70)
        assert self._handler(handlers).states_for(dev) == {"position": 70}
        assert self._handler(handlers).states_for(dev, {}) == {"position": 70}

    def test_the_invert_option_flips_the_reading(self, handlers):
        """A motor wired so Indigo brightness 100 drives the blind shut."""
        dev = DimmerDevice(1, "Blind", brightness=70)
        assert self._handler(handlers).states_for(dev, {"invert": True}) == {"position": 30}

    def test_only_a_literal_true_inverts(self, handlers):
        """A truthy-but-not-True option is a corrupt blob, not a polarity."""
        dev = DimmerDevice(1, "Blind", brightness=70)
        assert self._handler(handlers).states_for(dev, {"invert": "yes"}) == {"position": 70}

    def test_a_device_with_no_brightness_says_nothing(self, handlers):
        assert self._handler(handlers).states_for(DimmerDevice(1, "Blind", brightness=None)) == {}

    def test_dispatch_sets_brightness_directly(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Blind", brightness=0)
        self._handler(handlers).dispatch("goToPosition", {"position": 65}, dev)
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(dev, value=65)

    def test_dispatch_inverts_when_the_export_says_so(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Blind", brightness=0)
        self._handler(handlers).dispatch("goToPosition", {"position": 65}, dev, {"invert": True})
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(dev, value=35)

    def test_the_inversion_round_trips(self, handlers, mock_indigo_base):
        """Push then command must land back where it started, or the blind walks."""
        handler = self._handler(handlers)
        for brightness in range(0, 101):
            dev = DimmerDevice(1, "Blind", brightness=brightness)
            position = handler.states_for(dev, {"invert": True})["position"]
            handler.dispatch("goToPosition", {"position": position}, dev, {"invert": True})
            assert mock_indigo_base.dimmer.setBrightness.call_args.kwargs["value"] == brightness

    def test_dispatch_clamps_an_out_of_range_position(self, handlers, mock_indigo_base):
        dev = DimmerDevice(1, "Blind", brightness=0)
        self._handler(handlers).dispatch("goToPosition", {"position": 250}, dev)
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(dev, value=100)

    def test_dispatch_without_a_position_raises_rather_than_guessing(self, handlers):
        with pytest.raises(ValueError, match="goToPosition"):
            self._handler(handlers).dispatch("goToPosition", {}, DimmerDevice(1, "Blind"))

    def test_stop_motion_is_accepted_and_drives_nothing(self, handlers, mock_indigo_base):
        """Indigo has no stop for a dimmer-modelled covering (see the handler).

        Accepted, so ``export_bridge`` does not log "a command that role does not
        define" — which would read as a protocol bug rather than the documented
        limitation it is.
        """
        dev = DimmerDevice(1, "Blind", brightness=50)
        assert self._handler(handlers).dispatch("stopMotion", {}, dev) is not False
        mock_indigo_base.dimmer.setBrightness.assert_not_called()
        mock_indigo_base.device.turnOff.assert_not_called()

    def test_stop_motion_returns_a_reason_rather_than_a_bare_success(self, handlers):
        """PR #125 M1: "accepted" and "did something" are not the same outcome.

        It used to answer ``True``, which is what a ``setBrightness`` answers, so
        the one thing the user needed — that the blind is going to finish its
        travel because Indigo has no stop for a dimmer — reached nobody. The
        string is truthy, so every ``if not dispatch(...)`` call site is
        unaffected; ``export_bridge`` is what turns it into a log line.
        """
        outcome = self._handler(handlers).dispatch("stopMotion", {}, DimmerDevice(1, "Blind"))
        assert isinstance(outcome, str) and "no stop action" in outcome

    def test_a_command_that_did_something_still_answers_plain_true(self, handlers):
        """The tri-state's other two legs, so "truthy" is not the whole contract."""
        handler = self._handler(handlers)
        dev = DimmerDevice(1, "Blind", brightness=0)
        assert handler.dispatch("goToPosition", {"position": 65}, dev) is True
        assert handler.dispatch("lock", {}, dev) is False


# ---------------------------------------------------------------------------
# doorLock (E4)
# ---------------------------------------------------------------------------
class TestDoorLock:
    @staticmethod
    def _handler(handlers):
        return handlers.HANDLERS["doorLock"]

    def test_locked_follows_on_state(self, handlers):
        """on = locked, mirroring ``matter_handlers/door_lock.py`` exactly."""
        assert self._handler(handlers).states_for(RelayDevice(1, "Door", onState=True)) == \
            {"locked": True}
        assert self._handler(handlers).states_for(RelayDevice(1, "Door", onState=False)) == \
            {"locked": False}

    def test_an_unknown_state_is_omitted_not_reported_as_unlocked(self, handlers):
        assert self._handler(handlers).states_for(RelayDevice(1, "Door", onState=None)) == {}

    def test_lock_dispatches_the_indigo_lock_command(self, handlers, mock_indigo_base):
        dev = RelayDevice(1, "Door", onState=False)
        self._handler(handlers).dispatch("lock", {}, dev)
        mock_indigo_base.device.lock.assert_called_once_with(dev)

    def test_unlock_dispatches_the_indigo_unlock_command(self, handlers, mock_indigo_base):
        dev = RelayDevice(1, "Door", onState=True)
        self._handler(handlers).dispatch("unlock", {}, dev)
        mock_indigo_base.device.unlock.assert_called_once_with(dev)

    def test_a_lock_never_falls_back_to_turn_on(self, handlers, mock_indigo_base):
        """PRD §7: no fallback path with no lock semantics.

        ``indigo.device.lock`` needs ``IsLockSubType``; if the device is not one,
        IndigoServer refuses — which is the right place for that refusal.
        Quietly driving a relay the user declared a lock through ``turnOn``
        instead would work often enough to hide the misconfiguration.
        """
        self._handler(handlers).dispatch("lock", {}, RelayDevice(1, "Door"))
        mock_indigo_base.device.turnOn.assert_not_called()
        mock_indigo_base.device.turnOff.assert_not_called()

    def test_dispatch_confirms_nothing(self, handlers, mock_indigo_base):
        """No state write, no read-back, no synthesised confirmation."""
        dev = RelayDevice(1, "Door", onState=False)
        self._handler(handlers).dispatch("lock", {}, dev)
        assert dev.onState is False, "the handler wrote the state it was hoping for"
        mock_indigo_base.device.statusRequest.assert_not_called()


# ---------------------------------------------------------------------------
# thermostat (E4)
# ---------------------------------------------------------------------------
class TestThermostat:
    @staticmethod
    def _handler(handlers):
        return handlers.HANDLERS["thermostat"]

    @staticmethod
    def _dev(**attrs):
        import indigo
        attrs.setdefault("temperatures", [20.5])
        attrs.setdefault("heatSetpoint", 21.0)
        attrs.setdefault("coolSetpoint", 24.0)
        attrs.setdefault("hvacMode", indigo.kHvacMode.Heat)
        return ThermostatDevice(1, "Hall Stat", **attrs)

    def test_states_read_the_documented_thermostat_attributes(self, handlers):
        assert self._handler(handlers).states_for(self._dev()) == {
            "localTemperatureC": 20.5,
            "heatingSetpointC": 21.0,
            "coolingSetpointC": 24.0,
            "systemMode": "heat",
        }

    def test_the_first_temperature_sensor_is_the_matter_local_temperature(self, handlers):
        """Matter's Thermostat has exactly one; Indigo allows three."""
        states = self._handler(handlers).states_for(self._dev(temperatures=[19.0, 30.0, 5.0]))
        assert states["localTemperatureC"] == 19.0

    def test_an_empty_temperature_list_omits_the_key(self, handlers):
        assert "localTemperatureC" not in self._handler(handlers).states_for(
            self._dev(temperatures=[]))

    @pytest.mark.parametrize("attribute,expected", [
        ("Off", "off"), ("Heat", "heat"), ("Cool", "cool"), ("HeatCool", "auto"),
    ])
    def test_every_mode_in_the_4_2_domain_maps(self, handlers, attribute, expected):
        import indigo
        dev = self._dev(hvacMode=getattr(indigo.kHvacMode, attribute))
        assert self._handler(handlers).states_for(dev)["systemMode"] == expected

    @pytest.mark.parametrize("attribute,expected", [
        ("ProgramHeat", "heat"), ("ProgramCool", "cool"), ("ProgramHeatCool", "auto"),
    ])
    def test_the_program_modes_collapse_onto_their_base_mode(self, handlers, attribute,
                                                             expected):
        """Matter has no "running its own schedule" value.

        Reporting `off` for ProgramHeat would be a worse lie than reporting
        `heat` — which is, after all, what the thermostat is doing.
        """
        import indigo
        dev = self._dev(hvacMode=getattr(indigo.kHvacMode, attribute))
        assert self._handler(handlers).states_for(dev)["systemMode"] == expected

    def test_an_unreadable_mode_is_omitted_rather_than_guessed(self, handlers):
        assert "systemMode" not in self._handler(handlers).states_for(self._dev(hvacMode=None))

    def test_diff_reports_only_the_setpoint_that_moved(self, handlers):
        assert self._handler(handlers).diff(self._dev(), self._dev(heatSetpoint=22.5)) == \
            {"heatingSetpointC": 22.5}

    def test_setpoint_dispatch_uses_the_documented_indigo_commands(self, handlers,
                                                                   mock_indigo_base):
        dev = self._dev()
        handler = self._handler(handlers)
        handler.dispatch("setHeatingSetpoint", {"valueC": 21.5}, dev)
        handler.dispatch("setCoolingSetpoint", {"valueC": 23.0}, dev)
        mock_indigo_base.thermostat.setHeatSetpoint.assert_called_once_with(dev, value=21.5)
        mock_indigo_base.thermostat.setCoolSetpoint.assert_called_once_with(dev, value=23.0)

    def test_a_setpoint_is_passed_through_unconverted(self, handlers, mock_indigo_base):
        """§4.2 says °C; Indigo records no unit. See the module docstring."""
        dev = self._dev()
        self._handler(handlers).dispatch("setHeatingSetpoint", {"valueC": 18.3}, dev)
        mock_indigo_base.thermostat.setHeatSetpoint.assert_called_once_with(dev, value=18.3)

    @pytest.mark.parametrize("mode,attribute", [
        ("off", "Off"), ("heat", "Heat"), ("cool", "Cool"), ("auto", "HeatCool"),
    ])
    def test_mode_dispatch_uses_the_kHvacMode_members(self, handlers, mock_indigo_base,
                                                      mode, attribute):
        """`auto` is HeatCool, never ProgramHeatCool — the user asked for both
        setpoints to be honoured, not for the built-in programme to take over."""
        dev = self._dev()
        self._handler(handlers).dispatch("setSystemMode", {"systemMode": mode}, dev)
        mock_indigo_base.thermostat.setHvacMode.assert_called_once_with(
            dev, value=getattr(mock_indigo_base.kHvacMode, attribute))

    def test_a_mode_outside_the_domain_raises_rather_than_guessing(self, handlers):
        with pytest.raises(ValueError, match="§4.2 domain"):
            self._handler(handlers).dispatch("setSystemMode", {"systemMode": "dry"},
                                             self._dev())

    def test_a_setpoint_without_a_number_raises(self, handlers):
        with pytest.raises(ValueError, match="valueC"):
            self._handler(handlers).dispatch("setHeatingSetpoint", {"valueC": "warm"},
                                             self._dev())


# ---------------------------------------------------------------------------
# Sensors (E4)
# ---------------------------------------------------------------------------
class TestSensors:
    @pytest.mark.parametrize("role,key", [
        ("occupancySensor", "occupied"), ("contactSensor", "contact"),
    ])
    def test_a_binary_sensor_reads_on_state_without_inverting(self, handlers, role, key):
        """Both mirror the inbound handlers: `contact` true means CLOSED."""
        handler = handlers.HANDLERS[role]
        assert handler.states_for(SensorDevice(1, "S", onState=True)) == {key: True}
        assert handler.states_for(SensorDevice(1, "S", onState=False)) == {key: False}

    @pytest.mark.parametrize("role,key", [
        ("temperatureSensor", "temperatureC"), ("humiditySensor", "humidityPct"),
        ("lightSensor", "lux"), ("flowSensor", "flowM3h"),
    ])
    def test_a_numeric_sensor_reads_sensor_value_under_its_own_key(self, handlers, role, key):
        """``pressureSensor`` is deliberately absent — it is the one that converts."""
        handler = handlers.HANDLERS[role]
        assert handler.states_for(SensorDevice(1, "S", sensorValue=101.3)) == {key: 101.3}

    def test_pressure_converts_indigos_hpa_to_the_wire_s_kpa(self, handlers):
        """PR #125 H4: the factor of ten that still renders plausibly.

        Indigo's barometer unit is hPa — this plugin's own inbound handler
        writes ``sensorValue`` in hPa and labels it " hPa", and
        ``export_catalog`` routes ``hpa``/``mbar`` device names to this role —
        while §4.2 names the wire key ``pressureKPa``. Passing the reading
        through unconverted sent sea-level 1013 hPa out as 1013 kPa, which the
        node scaled to 10130 and every ecosystem drew as ten atmospheres.
        """
        handler = handlers.HANDLERS["pressureSensor"]
        assert handler.states_for(SensorDevice(1, "S", sensorValue=1013)) == {"pressureKPa": 101.3}
        # Rounded to the 0.1 kPa the wire actually carries, not beyond it.
        assert handler.states_for(SensorDevice(1, "S", sensorValue=1013.7)) == {"pressureKPa": 101.4}
        assert handler.states_for(SensorDevice(1, "S", sensorValue=None)) == {}

    def test_an_integer_reading_is_reported_as_a_float(self, handlers):
        """§4.2 types these as float; the node's converters take numbers either way."""
        states = handlers.HANDLERS["lightSensor"].states_for(SensorDevice(1, "S",
                                                                         sensorValue=320))
        assert states == {"lux": 320.0}

    def test_a_boolean_is_not_a_sensor_value(self, handlers):
        """`True` is an int in Python; reporting it as 1 lux would be nonsense."""
        assert handlers.HANDLERS["lightSensor"].states_for(
            SensorDevice(1, "S", sensorValue=True)) == {}

    def test_a_reading_that_did_not_move_pushes_nothing(self, handlers):
        handler = handlers.HANDLERS["temperatureSensor"]
        assert handler.diff(SensorDevice(1, "S", sensorValue=21.5),
                            SensorDevice(1, "S", sensorValue=21.5)) == {}

    def test_a_reading_has_no_tolerance_because_nothing_round_trips(self, handlers):
        """Unlike hue: Matter sensors are read-only, so there is no echo to damp."""
        handler = handlers.HANDLERS["temperatureSensor"]
        assert handler.diff(SensorDevice(1, "S", sensorValue=21.5),
                            SensorDevice(1, "S", sensorValue=21.6)) == {"temperatureC": 21.6}

    def test_a_sensor_dispatches_nothing_at_all(self, handlers):
        for role in ("occupancySensor", "contactSensor", "temperatureSensor",
                     "humiditySensor", "lightSensor", "pressureSensor", "flowSensor"):
            assert handlers.HANDLERS[role].dispatch("onOff", {"value": True},
                                                    SensorDevice(1, "S")) is False


# ---------------------------------------------------------------------------
# A published key that VANISHES (PR #125 C4)
# ---------------------------------------------------------------------------
class TestKeysThatStopBeingReported:
    """A device that stops answering a key it used to publish.

    ``states_for`` correctly omits it — §3.4 maps are partial and a fabricated
    0/False would be pushed to every ecosystem as fact — so ``diff`` iterating
    ``after`` finds nothing to say. §4.2 has no spelling for "I no longer know",
    and a reconnect does not heal it (``attach`` sends the same partial snapshot
    and §3.4 leaves absent keys untouched), so the ecosystem keeps the last
    value it was told forever.

    The decision is to keep that last known good value and make the *gap*
    visible instead. These pin both halves.
    """

    def test_a_sensor_going_quiet_pushes_nothing(self, handlers):
        """A flat battery: pushing a fabricated 0 °C would be worse than stale."""
        handler = handlers.HANDLERS["temperatureSensor"]
        before = SensorDevice(1, "S", sensorValue=21.5)
        after = SensorDevice(1, "S", sensorValue=None)
        assert handler.diff(before, after) == {}

    def test_a_lock_going_quiet_pushes_nothing(self, handlers):
        """The one that matters: no invented "Unlocked" for a bolt nobody can read."""
        handler = handlers.HANDLERS["doorLock"]
        before = RelayDevice(1, "Front Door", onState=True)
        after = RelayDevice(1, "Front Door", onState=None)
        assert handler.diff(before, after) == {}

    @pytest.mark.parametrize("role,dev_before,dev_after,key", [
        ("temperatureSensor", lambda: SensorDevice(1, "S", sensorValue=21.5),
         lambda: SensorDevice(1, "S", sensorValue=None), "temperatureC"),
        ("doorLock", lambda: RelayDevice(1, "D", onState=True),
         lambda: RelayDevice(1, "D", onState=None), "locked"),
    ])
    def test_the_gap_is_reported_to_the_caller(self, handlers, role, dev_before, dev_after, key):
        """Silent is the failure mode; ``diff_with_gaps`` is the way out of it."""
        changed, stopped = handlers.HANDLERS[role].diff_with_gaps(dev_before(), dev_after())
        assert changed == {}
        assert stopped == frozenset({key})

    def test_a_key_that_came_back_is_not_a_gap(self, handlers):
        handler = handlers.HANDLERS["temperatureSensor"]
        changed, stopped = handler.diff_with_gaps(SensorDevice(1, "S", sensorValue=None),
                                                  SensorDevice(1, "S", sensorValue=4.0))
        assert changed == {"temperatureC": 4.0}
        assert stopped == frozenset()

    def test_an_ordinary_change_reports_no_gap(self, handlers):
        """The common path must not start claiming devices have gone quiet."""
        handler = handlers.HANDLERS["dimmableLight"]
        changed, stopped = handler.diff_with_gaps(DimmerDevice(1, "L", onState=True, brightness=40),
                                                  DimmerDevice(1, "L", onState=True, brightness=75))
        assert changed == {"level": 75}
        assert stopped == frozenset()


# ---------------------------------------------------------------------------
# Battery (issue #220) — role-independent, added only by `published_states`
# ---------------------------------------------------------------------------
class TestBattery:
    @pytest.mark.parametrize("value,expected", [
        (None, None),
        (0, None),      # issue #190: indistinguishable from "never polled"
        (0.0, None),
        (0.4, None),    # rounds to 0 — the old raw `== 0` check let this through
        (0.49, None),
        (0.5, None),    # Python's banker's rounding also lands on 0
        (-1, None),     # a driver's "unknown" sentinel, not a real reading
        (-0.2, None),
        (True, None),   # bool is an int in Python; not a battery reading
        ("77", None),
        (1, 1),
        (0.51, 1),      # rounds to 1
        (77, 77),
        (100.4, 100),
        (150, 100),     # clamped
    ])
    def test_battery_percent(self, handlers, value, expected):
        assert handlers.battery_percent(RelayDevice(1, "D", batteryLevel=value)) == expected

    def test_battery_percent_is_none_when_the_attribute_is_absent(self, handlers):
        assert handlers.battery_percent(RelayDevice(1, "D")) is None

    def test_an_overrange_reading_warns_once_per_device_not_once_per_call(self, handlers, caplog):
        dev = RelayDevice(42, "Leaky Sensor", batteryLevel=150)
        with caplog.at_level("WARNING"):
            assert handlers.battery_percent(dev) == 100
            assert handlers.battery_percent(dev) == 100
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "42" in warnings[0].message and "150" in warnings[0].message

    def test_the_overrange_latch_is_keyed_per_device(self, handlers, caplog):
        with caplog.at_level("WARNING"):
            handlers.battery_percent(RelayDevice(43, "A", batteryLevel=150))
            handlers.battery_percent(RelayDevice(44, "B", batteryLevel=150))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 2

    def test_an_in_range_reading_never_warns(self, handlers, caplog):
        with caplog.at_level("WARNING"):
            handlers.battery_percent(RelayDevice(45, "Normal", batteryLevel=80))
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_published_states_adds_battery_level_for_every_role_but_states_for_never_does(
            self, handlers, role):
        dev = _capable_device(role)
        dev.batteryLevel = 61
        handler = handlers.HANDLERS[role]
        assert handler.published_states(dev)["batteryLevel"] == 61
        assert "batteryLevel" not in handler.states_for(dev)

    def test_published_states_omits_the_key_when_there_is_no_reading(self, handlers):
        handler = handlers.HANDLERS["onOffLight"]
        dev = RelayDevice(1, "L", onState=True)
        assert "batteryLevel" not in handler.published_states(dev)

    def test_diff_from_reports_a_battery_change(self, handlers):
        handler = handlers.HANDLERS["onOffLight"]
        dev = RelayDevice(1, "L", onState=True, batteryLevel=80)
        pushed = handler.published_states(dev)
        after = RelayDevice(1, "L", onState=True, batteryLevel=79)
        changed, stopped = handler.diff_from(pushed, after)
        assert changed == {"batteryLevel": 79}
        assert stopped == frozenset()

    def test_diff_from_is_silent_when_battery_is_unchanged(self, handlers):
        handler = handlers.HANDLERS["onOffLight"]
        dev = RelayDevice(1, "L", onState=True, batteryLevel=80)
        pushed = handler.published_states(dev)
        changed, _stopped = handler.diff_from(pushed, RelayDevice(1, "L", onState=True, batteryLevel=80))
        assert changed == {}

    def test_a_device_that_stops_reporting_battery_is_a_stopped_key_streak(self, handlers):
        handler = handlers.HANDLERS["onOffLight"]
        pushed = handler.published_states(RelayDevice(1, "L", onState=True, batteryLevel=80))
        after = RelayDevice(1, "L", onState=True, batteryLevel=None)
        changed, stopped = handler.diff_from(pushed, after)
        assert changed == {}
        assert stopped == frozenset({"batteryLevel"})


# ---------------------------------------------------------------------------
# Custom-state mapping (ADR-0012, issue #252)
# ---------------------------------------------------------------------------
class TestMappedBinarySensor:
    """A binary sensor whose reading is a plugin-named state, not `onState`."""

    def test_the_mapped_state_is_published(self, handlers):
        handler = handlers.HANDLERS["occupancySensor"]
        dev = fakes.texecom_zone(status=True)
        assert handler.states_for(dev, {"stateKey": "status"}) == {"occupied": True}

    def test_the_mapping_can_be_inverted(self, handlers):
        """An alarm zone whose boolean is true when HEALTHY reads backwards
        without this — the tick-box is why the mapping is two questions."""
        handler = handlers.HANDLERS["contactSensor"]
        dev = fakes.texecom_zone(status=True)
        assert handler.states_for(dev, {"stateKey": "status", "stateInvert": True}) \
            == {"contact": False}

    def test_a_vanished_state_publishes_nothing(self, handlers):
        """Fail closed, exactly as an `onState` of None already does: a
        fabricated False would tell every ecosystem the smoke alarm is quiet."""
        handler = handlers.HANDLERS["smokeAlarm"]
        assert handler.states_for(fakes.texecom_zone(), {"stateKey": "gone"}) == {}

    def test_a_state_that_stopped_being_boolean_publishes_nothing(self, handlers):
        handler = handlers.HANDLERS["occupancySensor"]
        dev = fakes.CustomDevice(1, "Odd", states={"status": "Healthy"})
        assert handler.states_for(dev, {"stateKey": "status"}) == {}

    def test_no_mapping_still_reads_onstate(self, handlers):
        """The standard path is untouched — every already-exported sensor
        keeps reading exactly what it read before."""
        handler = handlers.HANDLERS["occupancySensor"]
        assert handler.states_for(SensorDevice(1, "PIR", supportsOnState=True, onState=True)) \
            == {"occupied": True}

    def test_an_empty_mapping_is_not_a_mapping(self, handlers):
        """A blank menu selection arrives as "" and must fall back to
        `onState`, not be read as a state named "" that has vanished."""
        handler = handlers.HANDLERS["occupancySensor"]
        dev = SensorDevice(1, "PIR", supportsOnState=True, onState=True)
        assert handler.states_for(dev, {"stateKey": ""}) == {"occupied": True}

    def test_a_mapped_change_is_diffed_like_any_other(self, handlers):
        """The push path needs nothing new: `device_updated` diffs published
        states, and a mapped reading is an ordinary published state."""
        handler = handlers.HANDLERS["occupancySensor"]
        options = {"stateKey": "status"}
        pushed = handler.published_states(fakes.texecom_zone(status=False), options)
        changed, stopped = handler.diff_from(pushed, fakes.texecom_zone(status=True), options)
        assert changed == {"occupied": True}
        assert stopped == frozenset()
