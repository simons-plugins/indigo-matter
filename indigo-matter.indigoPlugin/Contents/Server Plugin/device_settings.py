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

from matter_handlers.settings import (
    Bounds,
    DeviceSetting,
    coerce_value,
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


def offered_settings(device_type_id: str, limits: LimitsLookup) -> list[OfferedSetting]:
    """Which of the type's declared settings THIS unit implements.

    The gate is the limits attribute: a setting whose limits cannot be read and
    parsed is not offered at all. That is the honest-degradation rule — a pre-1.4
    occupancy sensor has neither HoldTime nor HoldTimeLimits, so it is shown no
    hold-time field rather than one that would silently fail — and it doubles as
    the guarantee that nothing is ever validated against a guessed range.
    """
    offered: list[OfferedSetting] = []
    for setting in settings_for_type(device_type_id):
        raw = limits(setting.cluster, setting.limits_attribute)
        if raw is None:
            continue
        bounds = setting.parse_limits(raw)
        if bounds is None:
            continue
        offered.append(OfferedSetting(setting, bounds))
    return offered


def config_ui_values(device_type_id: str, states: dict, limits: LimitsLookup,
                     values: Optional[dict] = None) -> dict:
    """Seed values for the dialog: markers, current values, range hints.

    Returns only the keys this module owns, for the caller to merge over the
    device's props — the props themselves are never the source of truth for a
    setting (the device is), so each open re-seeds from the live state rather
    than showing back whatever was typed last time.
    """
    out = dict(values or {})
    offered = offered_settings(device_type_id, limits)
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
        out[setting.key] = "" if current is None else str(current)
        if not setting.is_choice:
            out[f"{setting.key}Range"] = setting.describe_range(offer.bounds)
    return out


def _marker_field(setting: DeviceSetting) -> str:
    """The hidden ConfigUI field gating this setting's visibility.

    ``holdTime`` → ``hasHoldTime``. Mirrors the ids in Devices.xml; the two are
    kept in step by hand because Indigo builds ConfigUI from static XML and
    offers no runtime field injection.
    """
    if setting.key == "sensitivityLevel":
        return "hasSensitivity"  # predates the convention (issue #85's state id)
    return "has" + setting.key[0].upper() + setting.key[1:]


def validate_settings(device_type_id: str, values: dict,
                      limits: LimitsLookup) -> dict:
    """Bounds-check every offered setting. Returns ``{field_id: message}``.

    Synchronous and cheap by design: it runs when the user clicks Save, on the
    Indigo UI thread, and touches nothing but the cached bounds. The actual
    write — which can take seconds against a sleepy device — is deliberately not
    done here; see :func:`apply_setting`.
    """
    errors: dict = {}
    for offer in offered_settings(device_type_id, limits):
        setting = offer.setting
        if setting.key not in values:
            continue
        value, reason = coerce_value(setting, values.get(setting.key), offer.bounds)
        if value is None:
            errors[setting.key] = reason
    return errors


def planned_writes(device_type_id: str, values: dict, states: dict,
                   limits: LimitsLookup) -> list[PlannedWrite]:
    """The settings whose value the user actually CHANGED.

    Only changed settings are written. Re-sending an unchanged value on every
    Save would spend a multi-second round trip per setting against a
    battery-powered device to tell it what it already knows — and would turn a
    dialog opened to read a value into a write.
    """
    planned: list[PlannedWrite] = []
    for offer in offered_settings(device_type_id, limits):
        setting = offer.setting
        if setting.key not in values:
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
                        retry_delay: float = 2.0) -> tuple[bool, str]:
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

    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            await client.write(write)
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001 - retried below, reported if final
            last_error = exc
            if attempt == 1:
                await sleep(retry_delay)
    if last_error is not None:
        return False, (f"{setting.label} could not be sent to the device: {last_error}. "
                       f"The setting was not changed.")

    read_back: Any = None
    for attempt in (1, 2):
        try:
            read_back = await client.read(int(node_id), int(endpoint),
                                          setting.cluster, setting.attribute)
            break
        except Exception:  # noqa: BLE001 - an unreadable verify is a failure, below
            read_back = None
            if attempt == 1:
                await sleep(retry_delay)

    ok, reason = verify_write(setting, plan.value, read_back)
    if ok:
        return True, f"{setting.label} is now {plan.value}{setting.unit}."
    return False, reason
