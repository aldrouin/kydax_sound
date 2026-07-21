"""Diagnostics for Kydax Sound.

Downloadable from the integration page, so the recent traffic with the
appliances and the reasoning behind the active level can be inspected
without enabling debug logging.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import KydaxSoundConfigEntry
from .const import (
    CONF_MUSISELECT_HOST,
    channel_db_for_level,
    channel_level_for_db,
    channel_max_db,
    channel_position_for_level,
    position_to_db,
)

TO_REDACT = {"host", CONF_MUSISELECT_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> dict[str, Any]:
    """Everything needed to explain what the integration is doing."""
    hub = entry.runtime_data
    paused = hub.paused_channels

    channels = []
    for number, channel in hub.channels.items():
        position = hub.channel_positions.get(number)
        db = position_to_db(position) if position is not None else None
        entry_info: dict[str, Any] = {
            "number": number,
            "name": channel.get("name"),
            "configured_volumes": {
                f"{level}%": channel_db_for_level(channel, level)
                for level in hub.levels
            },
            "maximum_db": round(channel_max_db(channel), 1),
            "position_read": position,
            "db_read": None if db is None else round(db, 2),
            "paused": number in paused,
        }
        if db is not None:
            implied = channel_level_for_db(channel, db)
            entry_info["implied_level"] = (
                None if implied is None else round(implied, 2)
            )
            entry_info["expected_position_per_level"] = {
                f"{level}%": channel_position_for_level(channel, level)
                for level in hub.levels
            }
        channels.append(entry_info)

    return {
        "available": hub.available,
        "active_level": hub.active_level,
        "levels": hub.levels,
        "why_no_toggle_is_on": _explain(hub, channels),
        "channels": channels,
        "pause_groups": [
            {
                "name": group["name"],
                "channels": group["channels"],
                "active": hub.is_paused(group_id),
            }
            for group_id, group in hub.pause_groups.items()
        ],
        "events": {
            "running": list(hub.event_runs),
            "selected_language": hub.selected_language,
        },
        "symetrix_traffic": list(hub.symetrix.history),
        "musiselect_traffic": (
            list(hub.musiselect.history) if hub.musiselect else "not configured"
        ),
        "options": {
            key: value
            for key, value in entry.options.items()
            if key not in TO_REDACT
        },
    }


def _explain(hub, channels: list[dict]) -> str:
    """A sentence saying why the level toggles are on or off."""
    if not hub.channels:
        return "No channel is configured."
    if hub.available is not True:
        return "The appliance is not answering, so every entity is unavailable."
    if hub.active_level in hub.levels:
        return f"The {hub.active_level}% toggle is on."
    usable = [
        c for c in channels if not c["paused"] and c.get("implied_level") is not None
    ]
    if not usable:
        return "No channel reports a usable level (all paused, or all flat)."
    listing = ", ".join(f"{c['name']} at {c['implied_level']}%" for c in usable)
    if len({round(c["implied_level"]) for c in usable}) > 1:
        return (
            "The channels are not all at the same level, so their average of "
            f"{hub.active_level}% matches no toggle: {listing}."
        )
    return (
        f"The average is {hub.active_level}%, which is not one of the "
        f"configured levels {hub.levels}: {listing}."
    )
