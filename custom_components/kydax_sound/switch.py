"""Switches for Kydax Sound: one pause switch per configured group.

On = the group's channels are muted and locked (volume scenes skip them).
The pre-pause positions are kept as attributes so they survive HA restarts.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import KydaxSoundConfigEntry
from .const import CONF_PAUSE_GROUPS
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one pause switch per configured group."""
    hub = entry.runtime_data
    async_add_entities(
        KydaxSoundPauseSwitch(hub, group)
        for group in entry.options.get(CONF_PAUSE_GROUPS, [])
    )


class KydaxSoundPauseSwitch(KydaxSoundEntity, SwitchEntity, RestoreEntity):
    """On = these channels are muted and protected from volume changes."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, hub: KydaxSoundHub, group: dict) -> None:
        super().__init__(hub)
        self._group = group
        self._attr_name = group["name"]
        self._attr_unique_id = f"{hub.entry.entry_id}_pause_{group['id']}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            raw = last.attributes.get("saved_positions") or {}
            saved = {
                int(channel): int(position)
                for channel, position in raw.items()
                if str(position).lstrip("-").isdigit()
            }
            self._hub.seed_pause(self._group["id"], saved)

    @property
    def is_on(self) -> bool:
        return self._hub.is_paused(self._group["id"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"channels": self._group["channels"]}
        if self.is_on:
            attrs["saved_positions"] = {
                str(channel): position
                for channel, position in self._hub.saved_positions(
                    self._group["id"]
                ).items()
            }
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_set_pause(self._group["id"], True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_set_pause(self._group["id"], False)
