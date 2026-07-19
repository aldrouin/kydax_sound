"""Config and options flows for Kydax Sound."""

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
    CONF_PAUSE_GROUPS,
    CONF_VOLUME_SCENES,
    DEFAULT_PORT,
    DOMAIN,
    FADER_MAX_DB,
    FADER_MIN_DB,
)
from .symetrix import SymetrixClient, SymetrixError

CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            NumberSelector(
                NumberSelectorConfig(
                    min=1, max=65535, step=1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Coerce(int),
        ),
    }
)

# "7122, 7128 7134" -> [7122, 7128, 7134]
_CHANNEL_SPLIT = re.compile(r"[\s,;]+")
# one per line: "7122 = -24", "7122: -24" or "7122 -24" (comma decimals OK)
_LEVEL_LINE = re.compile(r"^(\d+)\s*(?:[=:]\s*|\s+)(-?\d+(?:[.,]\d+)?)$")


def _parse_channels(text: str) -> list[int] | None:
    """Parse a channel list; None if anything is invalid."""
    channels: list[int] = []
    for part in _CHANNEL_SPLIT.split(text.strip()):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= 10000:
            return None
        channels.append(int(part))
    return channels or None


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


def _format_channels(channels: list[int]) -> str:
    return ", ".join(str(channel) for channel in channels)


def _format_levels(levels: dict[str, float]) -> str:
    return "\n".join(f"{channel} = {level}" for channel, level in levels.items())


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
    """Initial setup: appliance address. One entry per appliance."""

    VERSION = 1

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
                return self.async_create_entry(
                    title=f"Symetrix {user_input[CONF_HOST]}",
                    data={},
                    options={
                        **user_input,
                        CONF_PAUSE_GROUPS: [],
                        CONF_VOLUME_SCENES: [],
                    },
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user", data_schema=CONNECTION_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KydaxSoundOptionsFlow:
        return KydaxSoundOptionsFlow()


class KydaxSoundOptionsFlow(OptionsFlow):
    """Ongoing management: connection, pause groups, volume scenes."""

    def __init__(self) -> None:
        self._edit_group_id: str | None = None
        self._edit_scene_id: str | None = None

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    def _save(self, new_options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=new_options)

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
            menu_options=["connection", "pause_groups", "volume_scenes"],
        )

    # --- connection ---------------------------------------------------------

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if await _async_try_connect(
                user_input[CONF_HOST], user_input[CONF_PORT]
            ):
                return self._save({**self._options, **user_input})
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(
                CONNECTION_SCHEMA, self._options
            ),
            errors=errors,
        )

    # --- pause groups ---------------------------------------------------------

    def _pause_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Required("channels"): TextSelector(),
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
            channels = _parse_channels(user_input["channels"])
            if channels is None:
                errors["channels"] = "invalid_channels"
            else:
                options = self._options
                groups = list(options.get(CONF_PAUSE_GROUPS, []))
                groups.append(
                    {
                        "id": uuid4().hex[:8],
                        "name": user_input["name"],
                        "channels": channels,
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
            channels = _parse_channels(user_input["channels"])
            if channels is None:
                errors["channels"] = "invalid_channels"
            else:
                updated = {
                    "id": current["id"],
                    "name": user_input["name"],
                    "channels": channels,
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
                    "channels": _format_channels(current["channels"]),
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
