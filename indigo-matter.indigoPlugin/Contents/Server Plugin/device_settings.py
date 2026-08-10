"""Edit Device dialog ↔ writable Matter settings (issue #186).

The machinery behind the Device Settings section of a Matter device's Edit
Device dialog: deciding which settings this particular unit can be offered,
seeding the dialog, validating what the user typed, and applying it.

Deliberately Indigo-free and matter-server-free. Everything here takes plain
dicts and injected lookups, so the whole flow — including the case that matters
most, a device that ACKs a write and ignores it — is unit-testable without a
Matter stack, an ``indigo`` module, or the one physical FP300 in the house.
``plugin.py`` holds the thin Indigo-facing callbacks that call in here.

**Why the dialog never reads live.** Opening an Edit Device dialog runs on the
Indigo UI thread, and a read from a sleepy Thread device is seconds (issue
#186). So the dialog is drawn from two cheap sources — the device's Indigo
*states* for current values (kept fresh by the subscription, so they also track
changes made from another ecosystem) and ``device_sync``'s cached limits for
ranges (firmware-fixed structure, which is exactly what a snapshot is good for).
A live read happens in one place only: verifying a write actually landed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from matter_client import ATTRIBUTE_TIMEOUT
from matter_handlers.settings import (
    NO_ANSWER,
    Bounds,
    FromAttribute,
    DeviceSetting,
    coerce_value,
    implements,
    settings_for_type,
    verify_write,
)
from protocol import MatterWrite

__all__ = [
    "OfferedSetting", "PlannedWrite", "MARKER_YES", "MARKER_NO",
    "offered_settings", "config_ui_values", "validate_settings",
    "planned_writes", "apply_setting",
]

#: Marker values for the ``hasX`` hidden ConfigUI fields that drive
#: ``visibleBindingValue``. That comparison is a CONTAINS match rather than
#: equality, so these two must never be substrings of one another.
MARKER_YES = "yes"
MARKER_NO = "no"


#: A limits lookup: ``(cluster, attribute) -> raw value or None``. Injected so
#: this module never has to know about device_sync or a node id.
LimitsLookup = Callable[[int, int], Any]


@dataclass(frozen=True)
class OfferedSetting:
    """A setting this specific device can actually be shown, with its bounds."""

    setting: DeviceSetting
    bounds: Bounds


@dataclass(frozen=True)
class PlannedWrite:
    """A setting the user changed, and the value to send."""

    setting: DeviceSetting
    value: int
    previous: Optional[int]


def offered_settings(device_type_id: str, limits: LimitsLookup,
                     attribute_list: Optional[Callable] = None,
                     device_known: bool = True,
                     on_skip: Optional[Callable] = None) -> list[OfferedSetting]:
    """Which of the type's declared settings THIS unit actually offers.

    Two independent gates, and they answer different questions:

    * **Does the device implement the attribute?** Its own AttributeList says
      so. This is the honest capability check and it works for every setting,
      including ones whose bounds are fixed by the spec rather than reported by
      the device. Of four plugs on the dev fabric, two implement OnOff's
      Lighting attributes and two do not — this is what tells them apart.
    * **What values are permitted?** The setting's declared bounds strategy.
      Unresolvable bounds still mean "do not offer", because validating against
      a guessed range is the thing the whole design refuses to do.

    An AttributeList that is missing or unreadable yields *unknown*, not *no* —
    the setting then stands or falls on its bounds alone, which is exactly the
    behaviour that shipped before the list was consulted. Hiding a setting a
    device really has would be as wrong as offering one it lacks.

    ``device_known=False`` means there is no Matter node behind this Indigo
    device yet, and nothing is offered. That has to be explicit: it used to fall
    out of the bounds being unresolvable, which stopped being true the moment a
    setting could take spec-fixed bounds. ``StaticRange``/``EnumValues`` resolve
    with no device at all, so a never-reconciled relay was shown a fully
    populated settings section, could not be saved past validation, and threw
    on the way out.

    ``on_skip(setting, reason)`` receives every setting that was NOT offered and
    why. Without it the caller sees only survivors, so a setting that vanished
    because of a shape change or a partial interview looks exactly like a
    feature that was never built — the shape of both bugs that shipped on this
    branch. This module stays logger-free; the caller decides how loud to be.
    """
    offered: list[OfferedSetting] = []

    def skip(setting, reason):
        if on_skip is not None:
            on_skip(setting, reason)

    for setting in settings_for_type(device_type_id):
        if not device_known:
            skip(setting, "the device has no Matter node yet")
            continue
        listed = attribute_list(setting.cluster) if attribute_list is not None else None
        known = implements(listed, setting.attribute)
        if known is False:
            skip(setting, "the device's AttributeList says it does not implement "
                          f"attribute 0x{setting.attribute:04X}")
            continue
        if known is None and not isinstance(setting.bounds, FromAttribute):
            # No positive evidence from EITHER source. FromAttribute settings
            # still have the limits attribute as evidence, but spec-fixed bounds
            # resolve unconditionally, so "unknown" would silently become
            # "offer it to everything" — including the plugs that demonstrably
            # do not implement it.
            skip(setting, "the device has not reported an AttributeList for cluster "
                          f"0x{setting.cluster:04X}, so there is no evidence it "
                          "implements this")
            continue
        bounds = setting.resolve_bounds(limits)
        if bounds is None:
            skip(setting, "its permitted values could not be determined "
                          "(limits attribute missing or unreadable)")
            continue
        offered.append(OfferedSetting(setting, bounds))
    return offered


def config_ui_values(device_type_id: str, states: dict, limits: LimitsLookup,
                     values: Optional[dict] = None,
                     attribute_list: Optional[Callable] = None,
                     device_known: bool = True,
                     on_skip: Optional[Callable] = None) -> dict:
    """Seed values for the dialog: markers, current values, range hints.

    Returns only the keys this module owns, for the caller to merge over the
    device's props — the props themselves are never the source of truth for a
    setting (the device is), so each open re-seeds from the live state rather
    than showing back whatever was typed last time.
    """
    out = dict(values or {})
    offered = offered_settings(device_type_id, limits, attribute_list,
                               device_known, on_skip)
    by_key = {o.setting.key: o for o in offered}

    for setting in settings_for_type(device_type_id):
        out[_marker_field(setting)] = MARKER_YES if setting.key in by_key else MARKER_NO
    out["hasAnySetting"] = MARKER_YES if offered else MARKER_NO

    for offer in offered:
        setting = offer.setting
        current = states.get(setting.key)
        # A menu's value must be a string to match its option ids; a text field
        # is a string either way. An unknown current value leaves the field
        # blank rather than inventing a plausible one.
        out[setting.key] = _seed_value(current)
        if not setting.is_choice:
            out[f"{setting.key}Range"] = setting.describe_range(offer.bounds)
    return out


def _seed_value(current: Any) -> str:
    """Render a state for the dialog.

    Integral values are rendered as ints so that merely opening and saving an
    untouched dialog always revalidates: a float-valued state seeded as "10.0"
    would be rejected by its own numeric validator as "must be a whole number",
    blaming the user for the plugin's own formatting.
    """
    if current is None:
        return ""
    if isinstance(current, bool):
        return str(int(current))
    if isinstance(current, float) and current.is_integer():
        return str(int(current))
    return str(current)


def _marker_field(setting: DeviceSetting) -> str:
    """The hidden ConfigUI field gating this setting's visibility.

    ``holdTime`` → ``hasHoldTime``. Mirrors the ids in Devices.xml; the two are
    kept in step by hand because Indigo builds ConfigUI from static XML and
    offers no runtime field injection.
    """
    if setting.key == "sensitivityLevel":
        return "hasSensitivity"  # predates the convention (issue #85's state id)
    return "has" + setting.key[0].upper() + setting.key[1:]


def validate_settings(device_type_id: str, values: dict, limits: LimitsLookup,
                      attribute_list: Optional[Callable] = None,
                      device_known: bool = True) -> dict:
    """Bounds-check every offered setting. Returns ``{field_id: message}``.

    Synchronous and cheap by design: it runs when the user clicks Save, on the
    Indigo UI thread, and touches nothing but the cached bounds. The actual
    write — which can take seconds against a sleepy device — is deliberately not
    done here; see :func:`apply_setting`.
    """
    errors: dict = {}
    for offer in offered_settings(device_type_id, limits, attribute_list, device_known):
        setting = offer.setting
        if setting.key not in values:
            continue
        # A BLANK field means "leave this alone", never "you must fill this in".
        # config_ui_values deliberately seeds blank when the device has not
        # reported the value yet, so rejecting blank made the Edit Device dialog
        # unsavable on a freshly adopted device — the user could not even rename
        # it, and the error blamed them for a value the plugin does not know
        # either. planned_writes skips blanks for the same reason.
        if not str(values.get(setting.key, "")).strip():
            continue
        value, reason = coerce_value(setting, values.get(setting.key), offer.bounds)
        if value is None:
            errors[setting.key] = reason
    return errors


def planned_writes(device_type_id: str, values: dict, states: dict,
                   limits: LimitsLookup,
                   attribute_list: Optional[Callable] = None,
                   device_known: bool = True) -> list[PlannedWrite]:
    """The settings whose value the user actually CHANGED.

    Only changed settings are written. Re-sending an unchanged value on every
    Save would spend a multi-second round trip per setting against a
    battery-powered device to tell it what it already knows — and would turn a
    dialog opened to read a value into a write.
    """
    planned: list[PlannedWrite] = []
    for offer in offered_settings(device_type_id, limits, attribute_list, device_known):
        setting = offer.setting
        if setting.key not in values:
            continue
        # A BLANK field means "leave this alone", never "you must fill this in".
        # config_ui_values deliberately seeds blank when the device has not
        # reported the value yet, so rejecting blank made the Edit Device dialog
        # unsavable on a freshly adopted device — the user could not even rename
        # it, and the error blamed them for a value the plugin does not know
        # either. planned_writes skips blanks for the same reason.
        if not str(values.get(setting.key, "")).strip():
            continue
        value, _reason = coerce_value(setting, values.get(setting.key), offer.bounds)
        if value is None:
            continue  # validate_settings already refused this; never send it
        previous = _as_int(states.get(setting.key))
        if previous is not None and previous == value:
            continue
        planned.append(PlannedWrite(setting, value, previous))
    return planned


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def apply_setting(client: Any, node_id: int, endpoint: int,
                        plan: PlannedWrite, *,
                        sleep: Callable = asyncio.sleep,
                        retry_delay: float = 2.0,
                        timeout: float = ATTRIBUTE_TIMEOUT,
                        on_observed: Optional[Callable] = None) -> tuple[bool, str]:
    """Write a setting and PROVE it landed. Returns ``(ok, message)``.

    Success means one thing only: a subsequent read returns the value we asked
    for. An ACK is not evidence — Aqara firmware acknowledges writes it then
    ignores — so this never reports success on the strength of the write alone.

    Both legs get one retry, because the failure being guarded against is a
    sleepy Thread device missing its poll window, not a device refusing. The
    write retry is safe to repeat: setting an attribute to a value is
    idempotent, so a write that timed out but actually landed costs nothing on
    the second attempt. A read that never answers is reported as a FAILURE with
    wording that says the outcome is unknown, rather than as a success — an
    unproven write reported as applied is the one outcome that makes the whole
    verification pointless.
    """
    setting = plan.setting
    write = MatterWrite(int(node_id), int(endpoint), setting.cluster,
                        setting.attribute, plan.value)

    # BOTH legs get the attribute timeout. The write used to fall through to
    # MatterClient.write's 5s default while only the read-back got 30s — on the
    # sleepy Thread device this feature targets (~9.5s poll interval) that
    # reported healthy hardware as "could not be sent", the exact false verdict
    # the whole design exists to prevent, and it returned before ever reading
    # back to find out the write had landed.
    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            await client.write(write, timeout=timeout)
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - retried below, reported if final
            last_error = exc
            if attempt == 1:
                await sleep(retry_delay)
    # A failed write is NOT evidence the setting is unchanged. A timeout can
    # land after matter-server or the device accepted it — which is the same
    # argument that justifies the long timeout above, so returning "the setting
    # was not changed" here would contradict it and assert an outcome nothing
    # verified. Fall through to the read-back and let the device answer; if the
    # transport really is down the read fails too and it is reported as
    # unconfirmed, which is the honest verdict.

    read_back: Any = NO_ANSWER
    read_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            read_back = await client.read(int(node_id), int(endpoint),
                                          setting.cluster, setting.attribute,
                                          timeout=timeout)
            read_error = None
            break
        except Exception as exc:  # noqa: BLE001 - an unreadable verify is a failure, below
            read_back = NO_ANSWER
            read_error = exc
            if attempt == 1:
                await sleep(retry_delay)

    # Hand the caller what the device ACTUALLY holds, when it said anything
    # readable. On a mismatch this is the authoritative value, and recording it
    # is what stops the dialog re-seeding a value the device never adopted.
    if on_observed is not None and read_back is not NO_ANSWER:
        try:
            on_observed(None if read_back is None else int(read_back))
        except (TypeError, ValueError):
            pass  # unreadable: verify_write below reports it; nothing to record

    ok, reason = verify_write(setting, plan.value, read_back)
    if ok:
        if last_error is not None:
            # The send errored and the value is on the device anyway — exactly
            # the case that made "could not be sent" a lie.
            return True, (f"{setting.label} is now {plan.value}{setting.unit} "
                          f"(the send reported an error — {last_error} — but the "
                          f"device holds the new value).")
        return True, reason or f"{setting.label} is now {plan.value}{setting.unit}."
    if last_error is not None:
        reason = f"{reason} The send also failed: {last_error}."
    if read_error is not None:
        # The write leg names its error; this one used to discard the exception
        # entirely, so a timeout, a dead socket and a protocol error all read as
        # one generic sentence. For a feature whose whole job is saying what
        # really happened, that is self-defeating.
        reason = f"{reason} (read-back failed: {read_error})"
    return False, reason
