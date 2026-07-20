"""Config and options flows for Kydax Sound.

Initial setup is a wizard: Symetrix connection (tested) -> MusiSelect
address (optional) -> channels, one form each, with their volume calibration
(dB at 50% and at 100%; other percentages are interpolated linearly in dB).
Everything remains editable afterwards through the options flow.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_CHANNELS,
    CONF_EVENT_BUTTONS,
    CONF_LEVELS,
    CONF_MUSISELECT_HOST,
    CONF_MUSISELECT_PORT,
    CONF_PAUSE_GROUPS,
    DEFAULT_LEVELS,
    DEFAULT_MUSISELECT_PORT,
    DEFAULT_PORT,
    DEFAULT_VOLUME_50,
    DEFAULT_VOLUME_100,
    DOMAIN,
    FADER_MAX_DB,
    FADER_MIN_DB,
    channel_db_for_pct,
)
from .symetrix import SymetrixClient, SymetrixError

_LIST_SPLIT = re.compile(r"[\s,;]+")


def _port_selector():
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Coerce(int),
    )


def _db_selector():
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=FADER_MIN_DB,
                max=FADER_MAX_DB,
                step=0.1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="dB",
            )
        ),
        vol.Coerce(float),
    )


CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): _port_selector(),
    }
)

MUSISELECT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MUSISELECT_HOST): TextSelector(),
        vol.Required(
            CONF_MUSISELECT_PORT, default=DEFAULT_MUSISELECT_PORT
        ): _port_selector(),
    }
)

CHANNEL_FIELDS = {
    vol.Required("volume_50", default=DEFAULT_VOLUME_50): _db_selector(),
    vol.Required("volume_100", default=DEFAULT_VOLUME_100): _db_selector(),
}

CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required("number"): vol.All(
            NumberSelector(
                NumberSelectorConfig(
                    min=1, max=10000, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Coerce(int),
        ),
        vol.Required("name"): TextSelector(),
        **CHANNEL_FIELDS,
    }
)

WIZARD_CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Optional("number"): vol.All(
            NumberSelector(
                NumberSelectorConfig(
                    min=1, max=10000, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Coerce(int),
        ),
        vol.Optional("name"): TextSelector(),
        **CHANNEL_FIELDS,
        vol.Required("add_another", default=True): BooleanSelector(),
    }
)

LEVELS_SCHEMA = vol.Schema({vol.Required(CONF_LEVELS): TextSelector()})


def _channel_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": user_input["number"],
        "name": user_input["name"].strip(),
        "volume_50": user_input["volume_50"],
        "volume_100": user_input["volume_100"],
    }


def _parse_level_list(text: str) -> list[int] | None:
    """Parse "0, 50, 60, 70" into a sorted unique list; None if invalid."""
    values: list[int] = []
    for part in _LIST_SPLIT.split(text.strip()):
        if not part:
            continue
        if not part.isdigit() or int(part) > 100:
            return None
        if int(part) not in values:
            values.append(int(part))
    return sorted(values) if values else None


def _format_level_list(levels: list[int]) -> str:
    return ", ".join(str(level) for level in levels)


def _clean_musiselect(options: dict[str, Any], user_input: dict[str, Any]) -> None:
    """Store the MusiSelect address; an empty host means no device."""
    host = (user_input.get(CONF_MUSISELECT_HOST) or "").strip()
    if host:
        options[CONF_MUSISELECT_HOST] = host
    else:
        options.pop(CONF_MUSISELECT_HOST, None)
    options[CONF_MUSISELECT_PORT] = user_input.get(
        CONF_MUSISELECT_PORT, DEFAULT_MUSISELECT_PORT
    )


async def _async_try_connect(host: str, port: int) -> bool:
    """Return True if the Symetrix appliance answers at host:port."""
    client = SymetrixClient(host, port)
    try:
        await client.async_ping()
    except SymetrixError:
        return False
    finally:
        client.disconnect()
    return True


class KydaxSoundConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup wizard: Symetrix -> MusiSelect -> channels (one form each)."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict[str, Any] = {}
        self._musiselect: dict[str, Any] = {}
        self._channels: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            if await _async_try_connect(
                user_input[CONF_HOST], user_input[CONF_PORT]
            ):
                self._connection = user_input
                return await self.async_step_musiselect()
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user", data_schema=CONNECTION_SCHEMA, errors=errors
        )

    async def async_step_musiselect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._musiselect = user_input
            return await self.async_step_channels()

        return self.async_show_form(
            step_id="musiselect", data_schema=MUSISELECT_SCHEMA
        )

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One channel per screen; uncheck "add another" to finish."""
        errors: dict[str, str] = {}
        if user_input is not None:
            number = user_input.get("number")
            name = (user_input.get("name") or "").strip()
            if number and name:
                if any(c["number"] == number for c in self._channels):
                    errors["number"] = "duplicate_channel"
                else:
                    self._channels.append(
                        _channel_from_input({**user_input, "name": name})
                    )
            elif number or name:
                errors["base"] = "channel_incomplete"
            if not errors:
                if user_input["add_another"]:
                    return self.async_show_form(
                        step_id="channels",
                        data_schema=WIZARD_CHANNEL_SCHEMA,
                        description_placeholders={
                            "count": str(len(self._channels))
                        },
                    )
                options: dict[str, Any] = {
                    **self._connection,
                    CONF_CHANNELS: self._channels,
                    CONF_LEVELS: DEFAULT_LEVELS,
                    CONF_PAUSE_GROUPS: [],
                    CONF_EVENT_BUTTONS: [],
                }
                _clean_musiselect(options, self._musiselect)
                return self.async_create_entry(
                    title=f"Symetrix {self._connection[CONF_HOST]}",
                    data={},
                    options=options,
                )

        return self.async_show_form(
            step_id="channels",
            data_schema=WIZARD_CHANNEL_SCHEMA,
            errors=errors,
            description_placeholders={"count": str(len(self._channels))},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KydaxSoundOptionsFlow:
        return KydaxSoundOptionsFlow()


class KydaxSoundOptionsFlow(OptionsFlow):
    """Ongoing management: connection, channels, levels, pauses, events."""

    def __init__(self) -> None:
        self._edit_group_id: str | None = None
        self._edit_event_id: str | None = None
        self._edit_channel_number: int | None = None

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    def _save(self, new_options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=new_options)

    def _channel_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(
                value=str(channel["number"]),
                label=f"{channel['name']} ({channel['number']})",
            )
            for channel in self._options.get(CONF_CHANNELS, [])
        ]

    def _group_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=group["id"], label=group["name"])
            for group in self._options.get(CONF_PAUSE_GROUPS, [])
        ]

    def _event_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=event["id"], label=event["name"])
            for event in self._options.get(CONF_EVENT_BUTTONS, [])
        ]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "connection",
                "channels",
                "levels",
                "pause_groups",
                "event_buttons",
            ],
        )

    # --- connection ---------------------------------------------------------

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = vol.Schema(
            {**CONNECTION_SCHEMA.schema, **MUSISELECT_SCHEMA.schema}
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            if await _async_try_connect(
                user_input[CONF_HOST], user_input[CONF_PORT]
            ):
                options = self._options
                options[CONF_HOST] = user_input[CONF_HOST]
                options[CONF_PORT] = user_input[CONF_PORT]
                _clean_musiselect(options, user_input)
                return self._save(options)
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(schema, self._options),
            errors=errors,
        )

    # --- channels -----------------------------------------------------------

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_channel"]
        if self._options.get(CONF_CHANNELS):
            menu += ["edit_channel", "remove_channel"]
        return self.async_show_menu(step_id="channels", menu_options=menu)

    async def async_step_add_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = self._options
            channels = list(options.get(CONF_CHANNELS, []))
            if any(c["number"] == user_input["number"] for c in channels):
                errors["number"] = "duplicate_channel"
            else:
                channels.append(_channel_from_input(user_input))
                options[CONF_CHANNELS] = channels
                return self._save(options)

        return self.async_show_form(
            step_id="add_channel", data_schema=CHANNEL_SCHEMA, errors=errors
        )

    async def async_step_edit_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_channel_number = int(user_input["channel"])
            return await self.async_step_edit_channel_form()

        return self.async_show_form(
            step_id="edit_channel",
            data_schema=vol.Schema(
                {
                    vol.Required("channel"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._channel_select_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    def _calibration_preview(self, channel: dict[str, Any]) -> str:
        """What dB this channel gets at each configured level, for the form."""
        parts = []
        for level in self._options.get(CONF_LEVELS, DEFAULT_LEVELS):
            db = channel_db_for_pct(
                channel.get("volume_50", DEFAULT_VOLUME_50),
                channel.get("volume_100", DEFAULT_VOLUME_100),
                level,
            )
            value = "off" if db is None else f"{round(db, 1)} dB"
            parts.append(f"{level}% → {value}")
        return " · ".join(parts)

    async def async_step_edit_channel_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self._options
        channels = list(options.get(CONF_CHANNELS, []))
        current = next(
            (c for c in channels if c["number"] == self._edit_channel_number),
            None,
        )
        if current is None:
            return await self.async_step_channels()

        errors: dict[str, str] = {}
        if user_input is not None:
            new_number = user_input["number"]
            if new_number != current["number"] and any(
                c["number"] == new_number for c in channels
            ):
                errors["number"] = "duplicate_channel"
            else:
                updated = _channel_from_input(user_input)
                options[CONF_CHANNELS] = [
                    updated if c["number"] == current["number"] else c
                    for c in channels
                ]
                if new_number != current["number"]:
                    # keep pause groups pointing at the renumbered channel
                    options[CONF_PAUSE_GROUPS] = [
                        {
                            **group,
                            "channels": [
                                new_number if n == current["number"] else n
                                for n in group["channels"]
                            ],
                        }
                        for group in options.get(CONF_PAUSE_GROUPS, [])
                    ]
                return self._save(options)

        return self.async_show_form(
            step_id="edit_channel_form",
            data_schema=self.add_suggested_values_to_schema(
                CHANNEL_SCHEMA, current
            ),
            errors=errors,
            description_placeholders={
                "name": current["name"],
                "preview": self._calibration_preview(current),
            },
        )

    async def async_step_remove_channel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options
            removed = {int(number) for number in user_input.get("channels", [])}
            options[CONF_CHANNELS] = [
                c
                for c in options.get(CONF_CHANNELS, [])
                if c["number"] not in removed
            ]
            options[CONF_PAUSE_GROUPS] = [
                {
                    **group,
                    "channels": [
                        n for n in group["channels"] if n not in removed
                    ],
                }
                for group in options.get(CONF_PAUSE_GROUPS, [])
            ]
            return self._save(options)

        return self.async_show_form(
            step_id="remove_channel",
            data_schema=vol.Schema(
                {
                    vol.Required("channels", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._channel_select_options(),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # --- volume levels --------------------------------------------------------

    async def async_step_levels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            levels = _parse_level_list(user_input[CONF_LEVELS])
            if levels is None:
                errors[CONF_LEVELS] = "invalid_levels"
            else:
                options = self._options
                options[CONF_LEVELS] = levels
                return self._save(options)

        return self.async_show_form(
            step_id="levels",
            data_schema=self.add_suggested_values_to_schema(
                LEVELS_SCHEMA,
                {
                    CONF_LEVELS: _format_level_list(
                        self._options.get(CONF_LEVELS, DEFAULT_LEVELS)
                    )
                },
            ),
            errors=errors,
        )

    # --- pause groups ---------------------------------------------------------

    def _pause_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Required("channels", default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=self._channel_select_options(),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

    async def async_step_pause_groups(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_pause"]
        if self._options.get(CONF_PAUSE_GROUPS):
            menu += ["edit_pause", "remove_pause"]
        return self.async_show_menu(step_id="pause_groups", menu_options=menu)

    async def async_step_add_pause(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("channels"):
                errors["channels"] = "channels_required"
            else:
                options = self._options
                groups = list(options.get(CONF_PAUSE_GROUPS, []))
                groups.append(
                    {
                        "id": uuid4().hex[:8],
                        "name": user_input["name"],
                        "channels": [int(number) for number in user_input["channels"]],
                    }
                )
                options[CONF_PAUSE_GROUPS] = groups
                return self._save(options)

        return self.async_show_form(
            step_id="add_pause", data_schema=self._pause_schema(), errors=errors
        )

    async def async_step_edit_pause(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_group_id = user_input["group"]
            return await self.async_step_edit_pause_form()

        return self.async_show_form(
            step_id="edit_pause",
            data_schema=vol.Schema(
                {
                    vol.Required("group"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._group_select_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_pause_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self._options
        groups = list(options.get(CONF_PAUSE_GROUPS, []))
        current = next(
            (g for g in groups if g["id"] == self._edit_group_id), None
        )
        if current is None:
            return await self.async_step_pause_groups()

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("channels"):
                errors["channels"] = "channels_required"
            else:
                updated = {
                    "id": current["id"],
                    "name": user_input["name"],
                    "channels": [int(number) for number in user_input["channels"]],
                }
                options[CONF_PAUSE_GROUPS] = [
                    updated if g["id"] == current["id"] else g for g in groups
                ]
                return self._save(options)

        return self.async_show_form(
            step_id="edit_pause_form",
            data_schema=self.add_suggested_values_to_schema(
                self._pause_schema(),
                {
                    "name": current["name"],
                    "channels": [str(number) for number in current["channels"]],
                },
            ),
            errors=errors,
        )

    async def async_step_remove_pause(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options
            removed = set(user_input.get("groups", []))
            options[CONF_PAUSE_GROUPS] = [
                g
                for g in options.get(CONF_PAUSE_GROUPS, [])
                if g["id"] not in removed
            ]
            return self._save(options)

        return self.async_show_form(
            step_id="remove_pause",
            data_schema=vol.Schema(
                {
                    vol.Required("groups", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._group_select_options(),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # --- event buttons --------------------------------------------------------

    def _event_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Optional("preset"): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=150, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional("command"): TextSelector(),
                vol.Required("delay", default=3): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=120,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional("duration"): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=3600,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional("return_preset"): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=150, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
            }
        )

    @staticmethod
    def _event_from_input(event_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {
            "id": event_id,
            "name": user_input["name"],
            "delay": user_input.get("delay", 3),
        }
        if user_input.get("preset"):
            event["preset"] = user_input["preset"]
        command = (user_input.get("command") or "").strip()
        if command:
            event["command"] = command
        if user_input.get("duration"):
            event["duration"] = user_input["duration"]
        if user_input.get("return_preset"):
            event["return_preset"] = user_input["return_preset"]
        return event

    @staticmethod
    def _event_action_valid(user_input: dict[str, Any]) -> bool:
        return bool(
            user_input.get("preset") or (user_input.get("command") or "").strip()
        )

    async def async_step_event_buttons(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_event"]
        if self._options.get(CONF_EVENT_BUTTONS):
            menu += ["edit_event", "remove_event"]
        return self.async_show_menu(step_id="event_buttons", menu_options=menu)

    async def async_step_add_event(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not self._event_action_valid(user_input):
                errors["base"] = "event_action_required"
            else:
                options = self._options
                events = list(options.get(CONF_EVENT_BUTTONS, []))
                events.append(self._event_from_input(uuid4().hex[:8], user_input))
                options[CONF_EVENT_BUTTONS] = events
                return self._save(options)

        return self.async_show_form(
            step_id="add_event", data_schema=self._event_schema(), errors=errors
        )

    async def async_step_edit_event(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_event_id = user_input["event"]
            return await self.async_step_edit_event_form()

        return self.async_show_form(
            step_id="edit_event",
            data_schema=vol.Schema(
                {
                    vol.Required("event"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._event_select_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_event_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self._options
        events = list(options.get(CONF_EVENT_BUTTONS, []))
        current = next(
            (e for e in events if e["id"] == self._edit_event_id), None
        )
        if current is None:
            return await self.async_step_event_buttons()

        errors: dict[str, str] = {}
        if user_input is not None:
            if not self._event_action_valid(user_input):
                errors["base"] = "event_action_required"
            else:
                updated = self._event_from_input(current["id"], user_input)
                options[CONF_EVENT_BUTTONS] = [
                    updated if e["id"] == current["id"] else e for e in events
                ]
                return self._save(options)

        return self.async_show_form(
            step_id="edit_event_form",
            data_schema=self.add_suggested_values_to_schema(
                self._event_schema(), current
            ),
            errors=errors,
        )

    async def async_step_remove_event(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options
            removed = set(user_input.get("events", []))
            options[CONF_EVENT_BUTTONS] = [
                e
                for e in options.get(CONF_EVENT_BUTTONS, [])
                if e["id"] not in removed
            ]
            return self._save(options)

        return self.async_show_form(
            step_id="remove_event",
            data_schema=vol.Schema(
                {
                    vol.Required("events", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._event_select_options(),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )
