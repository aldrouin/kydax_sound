"""Config and options flows for Kydax Sound.

Initial setup is a wizard: Symetrix connection (tested) -> MusiSelect
address (optional) -> channels with their default volume percentage.
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
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_CHANNELS,
    CONF_EVENT_BUTTONS,
    CONF_MUSISELECT_HOST,
    CONF_MUSISELECT_PORT,
    CONF_PAUSE_GROUPS,
    CONF_VOLUME_SCENES,
    DEFAULT_MUSISELECT_PORT,
    DEFAULT_PORT,
    DOMAIN,
    FADER_MAX_DB,
    FADER_MIN_DB,
)
from .symetrix import SymetrixClient, SymetrixError


def _port_selector():
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Coerce(int),
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

CHANNELS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CHANNELS, default=""): TextSelector(
            TextSelectorConfig(multiline=True)
        ),
    }
)

# one per line: "7122 = Bar = 70" (controller = name = default %)
_CHANNEL_DEF_LINE = re.compile(r"^(\d+)\s*=\s*(.+?)\s*=\s*(\d{1,3})$")
# one per line: "7122 = -24", "7122: -24" or "7122 -24" (comma decimals OK)
_LEVEL_LINE = re.compile(r"^(\d+)\s*(?:[=:]\s*|\s+)(-?\d+(?:[.,]\d+)?)$")


def _parse_channel_defs(text: str) -> list[dict[str, Any]] | None:
    """Parse channel definition lines; None if anything is invalid."""
    channels: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _CHANNEL_DEF_LINE.match(line)
        if match is None:
            return None
        number, name, pct = int(match.group(1)), match.group(2), int(match.group(3))
        if not 1 <= number <= 10000 or not 0 <= pct <= 100 or number in seen:
            return None
        seen.add(number)
        channels.append({"number": number, "name": name, "default_pct": pct})
    return channels


def _format_channel_defs(channels: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{channel['number']} = {channel['name']} = {channel['default_pct']}"
        for channel in channels
    )


def _parse_levels(text: str) -> dict[str, float] | None:
    """Parse per-channel dB lines; None if anything is invalid."""
    levels: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _LEVEL_LINE.match(line)
        if match is None:
            return None
        channel, level = int(match.group(1)), float(match.group(2).replace(",", "."))
        if not 1 <= channel <= 10000 or not FADER_MIN_DB <= level <= FADER_MAX_DB:
            return None
        levels[str(channel)] = level
    return levels or None


def _format_levels(levels: dict[str, float]) -> str:
    return "\n".join(f"{channel} = {level}" for channel, level in levels.items())


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
    """Setup wizard: Symetrix -> MusiSelect -> channels."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection: dict[str, Any] = {}
        self._musiselect: dict[str, Any] = {}

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
        errors: dict[str, str] = {}
        if user_input is not None:
            channels = _parse_channel_defs(user_input.get(CONF_CHANNELS, ""))
            if channels is None:
                errors[CONF_CHANNELS] = "invalid_channel_defs"
            else:
                options: dict[str, Any] = {
                    **self._connection,
                    CONF_CHANNELS: channels,
                    CONF_PAUSE_GROUPS: [],
                    CONF_VOLUME_SCENES: [],
                    CONF_EVENT_BUTTONS: [],
                }
                _clean_musiselect(options, self._musiselect)
                return self.async_create_entry(
                    title=f"Symetrix {self._connection[CONF_HOST]}",
                    data={},
                    options=options,
                )

        return self.async_show_form(
            step_id="channels", data_schema=CHANNELS_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KydaxSoundOptionsFlow:
        return KydaxSoundOptionsFlow()


class KydaxSoundOptionsFlow(OptionsFlow):
    """Ongoing management: connection, channels, pause groups, volume scenes."""

    def __init__(self) -> None:
        self._edit_group_id: str | None = None
        self._edit_scene_id: str | None = None
        self._edit_event_id: str | None = None

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

    def _scene_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=scene["id"], label=scene["name"])
            for scene in self._options.get(CONF_VOLUME_SCENES, [])
        ]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "connection",
                "channels",
                "pause_groups",
                "volume_scenes",
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
        """Bulk edit of the channel list, prefilled with the current config."""
        errors: dict[str, str] = {}
        if user_input is not None:
            channels = _parse_channel_defs(user_input.get(CONF_CHANNELS, ""))
            if channels is None:
                errors[CONF_CHANNELS] = "invalid_channel_defs"
            else:
                options = self._options
                options[CONF_CHANNELS] = channels
                # Drop deleted channels from pause group scopes.
                numbers = {channel["number"] for channel in channels}
                options[CONF_PAUSE_GROUPS] = [
                    {
                        **group,
                        "channels": [
                            number
                            for number in group["channels"]
                            if number in numbers
                        ],
                    }
                    for group in options.get(CONF_PAUSE_GROUPS, [])
                ]
                return self._save(options)

        return self.async_show_form(
            step_id="channels",
            data_schema=self.add_suggested_values_to_schema(
                CHANNELS_SCHEMA,
                {
                    CONF_CHANNELS: _format_channel_defs(
                        self._options.get(CONF_CHANNELS, [])
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

    def _event_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=event["id"], label=event["name"])
            for event in self._options.get(CONF_EVENT_BUTTONS, [])
        ]

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

    # --- volume scenes ----------------------------------------------------------

    def _scene_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Required("levels"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }
        )

    def _scene_name_taken(self, name: str, scene_id: str | None) -> bool:
        return any(
            scene["name"] == name and scene["id"] != scene_id
            for scene in self._options.get(CONF_VOLUME_SCENES, [])
        )

    async def async_step_volume_scenes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_scene"]
        if self._options.get(CONF_VOLUME_SCENES):
            menu += ["edit_scene", "remove_scene"]
        return self.async_show_menu(step_id="volume_scenes", menu_options=menu)

    async def async_step_add_scene(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            levels = _parse_levels(user_input["levels"])
            if self._scene_name_taken(user_input["name"], None):
                errors["name"] = "name_exists"
            elif levels is None:
                errors["levels"] = "invalid_levels"
            else:
                options = self._options
                scenes = list(options.get(CONF_VOLUME_SCENES, []))
                scenes.append(
                    {
                        "id": uuid4().hex[:8],
                        "name": user_input["name"],
                        "levels": levels,
                    }
                )
                options[CONF_VOLUME_SCENES] = scenes
                return self._save(options)

        return self.async_show_form(
            step_id="add_scene", data_schema=self._scene_schema(), errors=errors
        )

    async def async_step_edit_scene(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_scene_id = user_input["scene"]
            return await self.async_step_edit_scene_form()

        return self.async_show_form(
            step_id="edit_scene",
            data_schema=vol.Schema(
                {
                    vol.Required("scene"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._scene_select_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_scene_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self._options
        scenes = list(options.get(CONF_VOLUME_SCENES, []))
        current = next(
            (s for s in scenes if s["id"] == self._edit_scene_id), None
        )
        if current is None:
            return await self.async_step_volume_scenes()

        errors: dict[str, str] = {}
        if user_input is not None:
            levels = _parse_levels(user_input["levels"])
            if self._scene_name_taken(user_input["name"], current["id"]):
                errors["name"] = "name_exists"
            elif levels is None:
                errors["levels"] = "invalid_levels"
            else:
                updated = {
                    "id": current["id"],
                    "name": user_input["name"],
                    "levels": levels,
                }
                options[CONF_VOLUME_SCENES] = [
                    updated if s["id"] == current["id"] else s for s in scenes
                ]
                return self._save(options)

        return self.async_show_form(
            step_id="edit_scene_form",
            data_schema=self.add_suggested_values_to_schema(
                self._scene_schema(),
                {
                    "name": current["name"],
                    "levels": _format_levels(current["levels"]),
                },
            ),
            errors=errors,
        )

    async def async_step_remove_scene(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options
            removed = set(user_input.get("scenes", []))
            options[CONF_VOLUME_SCENES] = [
                s
                for s in options.get(CONF_VOLUME_SCENES, [])
                if s["id"] not in removed
            ]
            return self._save(options)

        return self.async_show_form(
            step_id="remove_scene",
            data_schema=vol.Schema(
                {
                    vol.Required("scenes", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._scene_select_options(),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )
