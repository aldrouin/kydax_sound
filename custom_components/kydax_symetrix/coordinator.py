"""Hub for Kydax Symetrix: connection to the appliance and runtime state."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import POLL_SECONDS, signal_update

_LOGGER = logging.getLogger(__name__)


class KydaxSymetrixHub:
    """Holds the appliance connection and all runtime state.

    Same pattern as kydax_light's KydaxEngine: a poll loop instead of a
    heartbeat, entities subscribe via the dispatcher signal and never poll.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsubs: list[CALLBACK_TYPE] = []

    # --- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        """Connect and start polling."""
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=POLL_SECONDS)
            )
        )
        await self._async_poll(dt_util.now())

    @callback
    def async_stop(self) -> None:
        """Tear down listeners and the connection."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # --- polling -----------------------------------------------------------

    async def _async_poll(self, _now) -> None:
        """Read current values from the appliance.

        TODO: connect to the Symetrix control port and query the controller
        numbers we manage, so HA reflects reality after a restart or when
        someone changes volumes outside HA.
        """
        self._dispatch()

    # --- helpers -----------------------------------------------------------

    @callback
    def _dispatch(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
