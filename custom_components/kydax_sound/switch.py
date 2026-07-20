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
from .const import CONF_CHANNELS, CONF_EVENT_BUTTONS, CONF_PAUSE_GROUPS
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up pause, volume level and event switches."""
    hub = entry.runtime_data
    entities: list[SwitchEntity] = [
        KydaxSoundPauseSwitch(hub, group)
        for group in entry.options.get(CONF_PAUSE_GROUPS, [])
    ]
    if entry.options.get(CONF_CHANNELS):
        entities.extend(
            KydaxSoundLevelSwitch(hub, level) for level in hub.levels
        )
    entities.extend(
        KydaxSoundEventSwitch(hub, event)
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
    )
    async_add_entities(entities)


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


class KydaxSoundLevelSwitch(KydaxSoundEntity, SwitchEntity):
    """On when this percentage is the current sound level.

    Turning it on applies the level (each channel gets its calibrated dB,
    paused channels skipped). The active level is re-derived from the actual
    channel volumes every poll, so manual changes on the appliance are
    reflected here. Turning it off does nothing — activate another level
    instead.
    """

    _attr_translation_key = "volume_level"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, hub: KydaxSoundHub, level: int) -> None:
        super().__init__(hub)
        self._level = level
        self._attr_unique_id = f"{hub.entry.entry_id}_level_{level}"
        self._attr_translation_placeholders = {"level": str(level)}

    @property
    def is_on(self) -> bool:
        return self._hub.active_level == self._level

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The dB this level sends to each channel (dashboard visibility)."""
        return {"level": self._level, "values": self._hub.level_values(self._level)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_apply_level(self._level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        # a level is deactivated by activating another one
        self.async_write_ha_state()


class KydaxSoundEventSwitch(KydaxSoundEntity, SwitchEntity):
    """On while the event runs (e.g. birthday music); turn off to stop early.

    Turning on loads the Symetrix preset, waits the settle delay, sends the
    MusiSelect command, then after the configured duration loads the return
    preset. Turning off cancels and loads the return preset immediately.
    """

    _attr_icon = "mdi:party-popper"

    def __init__(self, hub: KydaxSoundHub, event: dict) -> None:
        super().__init__(hub)
        self._event = event
        self._attr_name = event["name"]
        self._attr_unique_id = f"{hub.entry.entry_id}_event_{event['id']}"

    @property
    def available(self) -> bool:
        # while an event runs, only its own switch stays usable (to cancel);
        # the other event switches are blocked
        if not super().available:
            return False
        return not self._hub.any_event_running or self.is_on

    @property
    def is_on(self) -> bool:
        return self._hub.is_event_running(self._event["id"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        finishes_at = self._hub.event_finishes_at(self._event["id"])
        if finishes_at is not None:
            attrs["finishes_at"] = finishes_at.isoformat()
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_trigger_event(self._event["id"])

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_cancel_event(self._event["id"])
