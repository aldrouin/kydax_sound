"""The Kydax Sound integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CHANNELS,
    CONF_EVENT_BUTTONS,
    CONF_LEVELS,
    CONF_PAUSE_GROUPS,
    DEFAULT_LEVELS,
    DOMAIN,
)
from .coordinator import KydaxSoundHub

SERVICE_SET_LEVEL = "set_level"
SET_LEVEL_SCHEMA = vol.Schema(
    {vol.Required("level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))}
)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type KydaxSoundConfigEntry = ConfigEntry[KydaxSoundHub]


async def async_setup_entry(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> bool:
    """Set up Kydax Sound from a config entry."""
    hub = KydaxSoundHub(hass, entry)
    entry.runtime_data = hub

    _async_prune_stale_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_LEVEL):
        return

    async def _async_handle_set_level(call: ServiceCall) -> None:
        """Apply a volume level (any %, interpolated) on every loaded entry."""
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await entry.runtime_data.async_apply_level(call.data["level"])

    hass.services.async_register(
        DOMAIN, SERVICE_SET_LEVEL, _async_handle_set_level, schema=SET_LEVEL_SCHEMA
    )


async def async_unload_entry(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> bool:
    """Unload a config entry."""
    entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> None:
    """Reload the entry when options change so entities match the config."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_prune_stale_entities(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> None:
    """Remove registry entries for pause groups/scenes deleted from options."""
    registry = er.async_get(hass)
    valid_ids = {
        f"{entry.entry_id}_pause_{group['id']}"
        for group in entry.options.get(CONF_PAUSE_GROUPS, [])
    }
    valid_ids.update(
        f"{entry.entry_id}_event_{event['id']}"
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
    )
    valid_ids.update(
        f"{entry.entry_id}_event_end_{event['id']}"
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        if event.get("duration")
    )
    if entry.options.get(CONF_CHANNELS):
        valid_ids.add(f"{entry.entry_id}_reset_volumes")
        levels = entry.options.get(CONF_LEVELS, DEFAULT_LEVELS)
        valid_ids.update(f"{entry.entry_id}_level_{level}" for level in levels)
        if levels:
            valid_ids.add(f"{entry.entry_id}_volume_level")
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            "_pause_" in reg_entry.unique_id
            or "_scene_" in reg_entry.unique_id
            or "_event_" in reg_entry.unique_id
            or "_level_" in reg_entry.unique_id
            or reg_entry.unique_id.endswith("_volume_scene")
            or reg_entry.unique_id.endswith("_volume_level")
            or reg_entry.unique_id.endswith("_reset_volumes")
        ) and reg_entry.unique_id not in valid_ids:
            registry.async_remove(reg_entry.entity_id)
