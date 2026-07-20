"""Select platform for Kydax Sound.

- the active volume level (percentage applied last; picking one applies it)
- one selector per event that defines options, e.g. the birthday song
  language: the choice made here is what the event switch sends.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import KydaxSoundConfigEntry
from .const import CONF_CHANNELS, CONF_EVENT_BUTTONS
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the volume level selector and per-event option selectors."""
    hub = entry.runtime_data
    entities: list[SelectEntity] = []
    if entry.options.get(CONF_CHANNELS) and hub.levels:
        entities.append(KydaxSoundVolumeLevelSelect(hub))
    entities.extend(
        KydaxSoundEventOptionSelect(hub, event)
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        if event.get("options")
    )
    entities.extend(
        KydaxSoundEventPresetSelect(hub, event)
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        if event.get("preset_options")
    )
    async_add_entities(entities)


class KydaxSoundEventOptionSelect(KydaxSoundEntity, SelectEntity, RestoreEntity):
    """Which variant an event sends — e.g. the birthday song language."""

    _attr_translation_key = "event_option"
    _attr_icon = "mdi:translate"

    def __init__(self, hub: KydaxSoundHub, event: dict) -> None:
        super().__init__(hub)
        self._event_id = event["id"]
        self._attr_unique_id = f"{hub.entry.entry_id}_event_option_{event['id']}"
        self._attr_translation_placeholders = {"event": event["name"]}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._hub.set_event_option(self._event_id, last.state)

    @property
    def options(self) -> list[str]:
        return self._hub.event_option_labels(self._event_id)

    @property
    def current_option(self) -> str | None:
        return self._hub.selected_event_option(self._event_id)

    async def async_select_option(self, option: str) -> None:
        self._hub.set_event_option(self._event_id, option)
        self.async_write_ha_state()


class KydaxSoundEventPresetSelect(KydaxSoundEntity, SelectEntity, RestoreEntity):
    """Which preset an event loads — typically the zone it plays in."""

    _attr_translation_key = "event_preset"
    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, hub: KydaxSoundHub, event: dict) -> None:
        super().__init__(hub)
        self._event_id = event["id"]
        self._attr_unique_id = f"{hub.entry.entry_id}_event_preset_{event['id']}"
        self._attr_translation_placeholders = {"event": event["name"]}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._hub.set_event_preset(self._event_id, last.state)

    @property
    def options(self) -> list[str]:
        return self._hub.event_preset_labels(self._event_id)

    @property
    def current_option(self) -> str | None:
        return self._hub.selected_event_preset(self._event_id)

    async def async_select_option(self, option: str) -> None:
        self._hub.set_event_preset(self._event_id, option)
        self.async_write_ha_state()


class KydaxSoundVolumeLevelSelect(KydaxSoundEntity, SelectEntity, RestoreEntity):
    """Shows the last applied volume level; selecting one applies it."""

    _attr_translation_key = "volume_level"
    _attr_icon = "mdi:volume-medium"

    def __init__(self, hub: KydaxSoundHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.entry.entry_id}_volume_level"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state.isdigit():
            self._hub.seed_level(int(last.state))

    @property
    def options(self) -> list[str]:
        return [str(level) for level in self._hub.levels]

    @property
    def current_option(self) -> str | None:
        # the set_level service accepts any %, which may not be in the list
        if self._hub.active_level not in self._hub.levels:
            return None
        return str(self._hub.active_level)

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"active_level": self._hub.active_level}
        if self._hub.active_level is not None:
            attrs["values"] = self._hub.level_values(self._hub.active_level)
        return attrs

    async def async_select_option(self, option: str) -> None:
        await self._hub.async_apply_level(int(option))
