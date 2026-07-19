"""Sensors for Kydax Sound: end-time of running events.

A timestamp sensor per event that has a duration — dashboards render it as a
live countdown ("in 2 minutes") while the event runs.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KydaxSoundConfigEntry
from .const import CONF_EVENT_BUTTONS
from .coordinator import KydaxSoundHub
from .entity import KydaxSoundEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KydaxSoundConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an end-time sensor for each event with a duration."""
    hub = entry.runtime_data
    async_add_entities(
        EventEndSensor(hub, event)
        for event in entry.options.get(CONF_EVENT_BUTTONS, [])
        if event.get("duration")
    )


class EventEndSensor(KydaxSoundEntity, SensorEntity):
    """When the running event will finish; unknown while idle."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "event_end"

    def __init__(self, hub: KydaxSoundHub, event: dict) -> None:
        super().__init__(hub)
        self._event = event
        self._attr_unique_id = f"{hub.entry.entry_id}_event_end_{event['id']}"
        self._attr_translation_placeholders = {"event": event["name"]}

    @property
    def native_value(self) -> datetime | None:
        return self._hub.event_finishes_at(self._event["id"])
