"""The Kydax Sound integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
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

SERVICE_TRIGGER_EVENT = "trigger_event"
SERVICE_CANCEL_EVENT = "cancel_event"
EVENT_NAME_SCHEMA = vol.Schema({vol.Required("name"): str})

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

    def _loaded_hubs() -> list[KydaxSoundHub]:
        return [
            entry.runtime_data
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]

    def _find_event(name: str) -> tuple[KydaxSoundHub, str]:
        for hub in _loaded_hubs():
            for event_id, event in hub.event_buttons.items():
                if event["name"] == name:
                    return hub, event_id
        raise ServiceValidationError(
            f"No event button named {name!r} is configured"
        )

    async def _async_handle_set_level(call: ServiceCall) -> None:
        """Apply a volume level (any %, interpolated) on every loaded entry."""
        for hub in _loaded_hubs():
            await hub.async_apply_level(call.data["level"])

    async def _async_handle_trigger_event(call: ServiceCall) -> None:
        """Start a configured event (preset -> delay -> command -> duration
        -> return preset) by its name."""
        hub, event_id = _find_event(call.data["name"])
        await hub.async_trigger_event(event_id)

    async def _async_handle_cancel_event(call: ServiceCall) -> None:
        """Stop a running event early; loads its return preset, if any."""
        hub, event_id = _find_event(call.data["name"])
        await hub.async_cancel_event(event_id)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_LEVEL, _async_handle_set_level, schema=SET_LEVEL_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRIGGER_EVENT,
        _async_handle_trigger_event,
        schema=EVENT_NAME_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_EVENT,
        _async_handle_cancel_event,
        schema=EVENT_NAME_SCHEMA,
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
        levels = entry.options.get(CONF_LEVELS, DEFAULT_LEVELS)
        valid_ids.update(f"{entry.entry_id}_level_{level}" for level in levels)
        if levels:
            valid_ids.add(f"{entry.entry_id}_volume_level")
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = reg_entry.unique_id
        # levels moved from buttons to switches in 0.5.0
        stale_level = "_level_" in unique_id and (
            reg_entry.domain != "switch" or unique_id not in valid_ids
        )
        stale_other = (
            "_pause_" in unique_id
            or "_scene_" in unique_id
            or "_event_" in unique_id
            or unique_id.endswith("_volume_scene")
            or unique_id.endswith("_volume_level")
            or unique_id.endswith("_reset_volumes")
        ) and unique_id not in valid_ids
        if stale_level or stale_other:
            registry.async_remove(reg_entry.entity_id)
