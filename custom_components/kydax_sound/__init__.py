"""The Kydax Sound integration."""

from __future__ import annotations

import logging
from copy import deepcopy

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import (
    CONF_CHANNELS,
    CONF_EVENT_BUTTONS,
    CONF_LEVELS,
    CONF_PAUSE_GROUPS,
    DEFAULT_LEVELS,
    DEFAULT_VOLUME_50,
    DEFAULT_VOLUME_100,
    DOMAIN,
)
from .coordinator import KydaxSoundHub

_LOGGER = logging.getLogger(__name__)

# options keys used by older versions, migrated away in _async_migrate_options
_LEGACY_VOLUME_SCENES = "volume_scenes"

SERVICE_SET_LEVEL = "set_level"
SET_LEVEL_SCHEMA = vol.Schema(
    {vol.Required("level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))}
)

SERVICE_SET_CHANNEL_LEVEL = "set_channel_level"
SET_CHANNEL_LEVEL_SCHEMA = vol.Schema(
    {
        vol.Required("channels"): vol.All(
            cv.ensure_list, [vol.Coerce(int)], vol.Length(min=1)
        ),
        vol.Required("level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    }
)

SERVICE_TRIGGER_EVENT = "trigger_event"
SERVICE_CANCEL_EVENT = "cancel_event"
EVENT_NAME_SCHEMA = vol.Schema({vol.Required("name"): str})

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type KydaxSoundConfigEntry = ConfigEntry[KydaxSoundHub]


async def async_setup_entry(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> bool:
    """Set up Kydax Sound from a config entry."""
    _async_migrate_options(hass, entry)

    hub = KydaxSoundHub(hass, entry)
    entry.runtime_data = hub

    _async_prune_stale_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def _async_migrate_options(
    hass: HomeAssistant, entry: KydaxSoundConfigEntry
) -> None:
    """Bring options saved by older versions up to the current schema.

    Idempotent: it runs on every setup and only writes when something
    actually changed, so entries created at any past version keep working
    without being reconfigured.
    """
    options = dict(entry.options)
    before = deepcopy(options)

    legacy_scenes = options.pop(_LEGACY_VOLUME_SCENES, None)

    # channels: {number, name, volume_50, volume_100}. Older versions stored
    # a default percentage instead of the calibration, with the dB values
    # living in volume scenes named after the percentages.
    channels = []
    for channel in options.get(CONF_CHANNELS, []):
        channel = dict(channel)
        channel.pop("default_pct", None)
        for key, level, fallback in (
            ("volume_50", 50, DEFAULT_VOLUME_50),
            ("volume_100", 100, DEFAULT_VOLUME_100),
        ):
            if channel.get(key) is None:
                channel[key] = _legacy_level_db(
                    legacy_scenes, channel.get("number"), level, fallback
                )
        channels.append(channel)
    if channels or CONF_CHANNELS in options:
        options[CONF_CHANNELS] = channels

    # events: the settle delay was removed once preset and command started
    # going out back-to-back
    events = []
    for event in options.get(CONF_EVENT_BUTTONS, []):
        event = dict(event)
        event.pop("delay", None)
        events.append(event)
    options[CONF_EVENT_BUTTONS] = events

    options.setdefault(CONF_LEVELS, DEFAULT_LEVELS)
    options.setdefault(CONF_PAUSE_GROUPS, [])

    if options != before:
        _LOGGER.info("Migrated stored configuration to the current schema")
        hass.config_entries.async_update_entry(entry, options=options)


def _legacy_level_db(
    scenes: list[dict] | None, number: int | None, level: int, fallback: float
) -> float:
    """Recover a channel's dB at a percentage from pre-0.2 volume scenes."""
    for scene in scenes or []:
        if str(scene.get("name", "")).strip() == str(level):
            value = (scene.get("levels") or {}).get(str(number))
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    break
    return fallback


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

    async def _async_handle_set_channel_level(call: ServiceCall) -> None:
        """Set the given channels to a percentage (each via its calibration)."""
        numbers = call.data["channels"]
        for hub in _loaded_hubs():
            mine = [n for n in numbers if n in hub.channels]
            if mine:
                await hub.async_set_channels_pct(mine, call.data["level"])

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
        SERVICE_SET_CHANNEL_LEVEL,
        _async_handle_set_channel_level,
        schema=SET_CHANNEL_LEVEL_SCHEMA,
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
    valid_ids.update(
        f"{entry.entry_id}_event_option_{event['id']}"
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        if event.get("options")
    )
    if entry.options.get(CONF_CHANNELS):
        levels = entry.options.get(CONF_LEVELS, DEFAULT_LEVELS)
        valid_ids.update(f"{entry.entry_id}_level_{level}" for level in levels)
        if levels:
            valid_ids.add(f"{entry.entry_id}_volume_level")
    valid_ids.update(
        f"{entry.entry_id}_channel_{channel['number']}"
        for channel in entry.options.get(CONF_CHANNELS, [])
    )
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
            or "_channel_" in unique_id
            or unique_id.endswith("_volume_scene")
            or unique_id.endswith("_volume_level")
            or unique_id.endswith("_reset_volumes")
            # flash button became a Tests option-menu action in 0.7.0
            or unique_id.endswith("_flash_unit")
        ) and unique_id not in valid_ids
        if stale_level or stale_other:
            registry.async_remove(reg_entry.entity_id)
