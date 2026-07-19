"""The Kydax Symetrix integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import KydaxSymetrixHub

# Add platforms here as they are built (e.g. Platform.MEDIA_PLAYER,
# Platform.NUMBER for volumes, Platform.SWITCH for mutes).
PLATFORMS: list = []

type KydaxSymetrixConfigEntry = ConfigEntry[KydaxSymetrixHub]


async def async_setup_entry(
    hass: HomeAssistant, entry: KydaxSymetrixConfigEntry
) -> bool:
    """Set up Kydax Symetrix from a config entry."""
    hub = KydaxSymetrixHub(hass, entry)
    entry.runtime_data = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: KydaxSymetrixConfigEntry
) -> bool:
    """Unload a config entry."""
    entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: KydaxSymetrixConfigEntry
) -> None:
    """Reload the entry when options change so entities match the config."""
    await hass.config_entries.async_reload(entry.entry_id)
