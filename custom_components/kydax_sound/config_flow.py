"""Config and options flows for Kydax Sound.

Initial setup is a wizard: Symetrix connection (tested) -> MusiSelect
address (optional) -> channels, one form each, with their volume calibration
(dB at 50% and at 100%; other percentages are interpolated linearly in dB).
Everything remains editable afterwards through the options flow.
"""

from __future__ import annotations

import json
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
    TextSelectorConfig,
)

from .const import (
    CONF_CHANNELS,
    CONF_CHANNEL_GROUPS,
    CONF_EVENT_BUTTONS,
    CONF_LEVELS,
    CONF_MUSISELECT_HOST,
    CONF_MUSISELECT_PORT,
    CONF_PAUSE_GROUPS,
    DEFAULT_LEVEL_DB,
    DEFAULT_LEVELS,
    DEFAULT_MUSISELECT_PORT,
    DEFAULT_PORT,
    DOMAIN,
    FADER_MAX_DB,
    FADER_MIN_DB,
    channel_db_for_level,
    channel_level_table,
)
from .symetrix import SymetrixClient, SymetrixError

_LIST_SPLIT = re.compile(r"[\s,;]+")

# import/export: everything that describes a site, without its addresses so
# a file can be reused as a template for another restaurant
PORTABLE_KEYS = (
    CONF_CHANNELS,
    CONF_LEVELS,
    CONF_CHANNEL_GROUPS,
    CONF_PAUSE_GROUPS,
    CONF_EVENT_BUTTONS,
)
DEFAULT_CONFIG_FILE = "kydax_sound.json"


def _export_payload(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "kydax_sound": {
            key: options.get(key) for key in PORTABLE_KEYS if key in options
        }
    }


def _validate_payload(payload: Any) -> str | None:
    """Return an error key when the imported content is unusable."""
    if not isinstance(payload, dict):
        return "invalid_file"
    channels = payload.get(CONF_CHANNELS)
    if channels is not None:
        if not isinstance(channels, list):
            return "invalid_file"
        for channel in channels:
            if (
                not isinstance(channel, dict)
                or not isinstance(channel.get("number"), int)
                or not str(channel.get("name") or "").strip()
                or not isinstance(channel.get("levels"), dict)
            ):
                return "invalid_channels"
    levels = payload.get(CONF_LEVELS)
    if levels is not None and (
        not isinstance(levels, list)
        or not all(isinstance(level, int) and 0 <= level <= 100 for level in levels)
    ):
        return "invalid_levels"
    for key in (CONF_CHANNEL_GROUPS, CONF_PAUSE_GROUPS, CONF_EVENT_BUTTONS):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(item, dict) and item.get("id") for item in value)
        ):
            return "invalid_file"
    return None


def _port_selector():
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
        ),
        vol.Coerce(int),
    )


def _optional_int(minimum: int, maximum: int, unit: str | None = None):
    """An optional whole number that may also be cleared (submitted as None)."""
    config = NumberSelectorConfig(
        min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return vol.Any(None, vol.All(NumberSelector(config), vol.Coerce(int)))


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
        # deploy before the appliance is reachable: entities stay
        # unavailable until it answers
        vol.Required("skip_test", default=False): BooleanSelector(),
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

def _controller_selector():
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=1, max=10000, step=1, mode=NumberSelectorMode.BOX
            )
        ),
        vol.Coerce(int),
    )


def _level_db_fields(levels: list[int]) -> dict[Any, Any]:
    """One dB field per configured level (0% is always off)."""
    return {
        vol.Required(f"db_{level}", default=DEFAULT_LEVEL_DB): _db_selector()
        for level in sorted(levels)
        if level > 0
    }


def _channel_schema(levels: list[int], optional_identity: bool = False) -> vol.Schema:
    identity: dict[Any, Any] = (
        {
            vol.Optional("number"): _controller_selector(),
            vol.Optional("name"): TextSelector(),
        }
        if optional_identity
        else {
            vol.Required("number"): _controller_selector(),
            vol.Required("name"): TextSelector(),
        }
    )
    return vol.Schema({**identity, **_level_db_fields(levels)})


def _wizard_channel_schema(levels: list[int]) -> vol.Schema:
    return vol.Schema(
        {
            **_channel_schema(levels, optional_identity=True).schema,
            vol.Required("add_another", default=True): BooleanSelector(),
        }
    )

LEVELS_SCHEMA = vol.Schema({vol.Required(CONF_LEVELS): TextSelector()})


def _channel_from_input(
    user_input: dict[str, Any], levels: list[int]
) -> dict[str, Any]:
    """Build a channel from the form: identity plus its dB per level."""
    return {
        "number": user_input["number"],
        "name": user_input["name"].strip(),
        "levels": {
            str(level): user_input[f"db_{level}"]
            for level in sorted(levels)
            if level > 0 and f"db_{level}" in user_input
        },
    }


def _channel_suggested(channel: dict[str, Any], levels: list[int]) -> dict[str, Any]:
    """Prefill values for the channel form from a stored channel."""
    table = channel_level_table(channel)
    suggested: dict[str, Any] = {
        "number": channel.get("number"),
        "name": channel.get("name"),
    }
    for level in sorted(levels):
        if level > 0:
            suggested[f"db_{level}"] = table.get(level, DEFAULT_LEVEL_DB)
    return suggested


class InvalidCommands(ValueError):
    """The MusiSelect command field could not be parsed."""


def _parse_commands(text: str) -> tuple[str | None, list[dict[str, str]] | None]:
    """Parse the MusiSelect command field.

    One line without "=" is a single command. Lines written as
    "Label = command" become a choice the user picks before running the
    event (e.g. the song language). Returns (command, choices) with at most
    one set; (None, None) when empty.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None, None
    if any("=" in line for line in lines):
        choices: list[dict[str, str]] = []
        for line in lines:
            label, sep, command = line.partition("=")
            if not sep or not label.strip() or not command.strip():
                raise InvalidCommands(line)
            choices.append({"label": label.strip(), "command": command.strip()})
        return None, choices
    if len(lines) > 1:
        # several commands with no labels: we would not know how to offer them
        raise InvalidCommands(lines[1])
    return lines[0], None


def _format_commands(event: dict[str, Any]) -> str:
    """The command field's text for an existing event."""
    if event.get("options"):
        return "\n".join(
            f"{o['label']} = {o['command']}" for o in event["options"]
        )
    return event.get("command", "")


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
            skip_test = user_input.pop("skip_test", False)
            if skip_test or await _async_try_connect(
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
        schema = _wizard_channel_schema(DEFAULT_LEVELS)
        errors: dict[str, str] = {}
        if user_input is not None:
            number = user_input.get("number")
            name = (user_input.get("name") or "").strip()
            if number and name:
                if any(c["number"] == number for c in self._channels):
                    errors["number"] = "duplicate_channel"
                else:
                    self._channels.append(
                        _channel_from_input(
                            {**user_input, "name": name}, DEFAULT_LEVELS
                        )
                    )
            elif number or name:
                errors["base"] = "channel_incomplete"
            if not errors:
                if user_input["add_another"]:
                    return self.async_show_form(
                        step_id="channels",
                        data_schema=schema,
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
            data_schema=schema,
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
        self._edit_cgroup_id: str | None = None

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
                "channel_groups",
                "levels",
                "pause_groups",
                "event_buttons",
                "backup",
                "tests",
            ],
        )

    # --- import / export ------------------------------------------------------

    async def async_step_backup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="backup", menu_options=["export", "import"]
        )

    async def async_step_export(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Write channels, levels, groups and events to a file."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = self.hass.config.path(user_input["path"])
            payload = _export_payload(self._options)

            def _write() -> None:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)

            try:
                await self.hass.async_add_executor_job(_write)
            except OSError:
                errors["path"] = "write_failed"
            else:
                return self.async_abort(
                    reason="exported", description_placeholders={"path": path}
                )

        return self.async_show_form(
            step_id="export",
            data_schema=vol.Schema(
                {
                    vol.Required("path", default=DEFAULT_CONFIG_FILE): TextSelector()
                }
            ),
            errors=errors,
        )

    async def async_step_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace channels, levels, groups and events from a file."""
        errors: dict[str, str] = {}
        if user_input is not None:
            path = self.hass.config.path(user_input["path"])

            def _read() -> Any:
                with open(path, encoding="utf-8") as handle:
                    return json.load(handle)

            try:
                data = await self.hass.async_add_executor_job(_read)
            except FileNotFoundError:
                errors["path"] = "file_not_found"
            except (OSError, ValueError):
                errors["path"] = "invalid_file"
            else:
                payload = data.get("kydax_sound", data) if isinstance(data, dict) else data
                problem = _validate_payload(payload)
                if problem:
                    errors["path"] = problem
                else:
                    options = self._options
                    for key in PORTABLE_KEYS:
                        if payload.get(key) is not None:
                            options[key] = payload[key]
                    return self._save(options)

        return self.async_show_form(
            step_id="import",
            data_schema=vol.Schema(
                {
                    vol.Required("path", default=DEFAULT_CONFIG_FILE): TextSelector()
                }
            ),
            errors=errors,
        )

    # --- channel groups -------------------------------------------------------

    def _cgroup_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=group["id"], label=group["name"])
            for group in self._options.get(CONF_CHANNEL_GROUPS, [])
        ]

    def _cgroup_schema(self) -> vol.Schema:
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

    async def async_step_channel_groups(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_cgroup"]
        if self._options.get(CONF_CHANNEL_GROUPS):
            menu += ["edit_cgroup", "remove_cgroup"]
        return self.async_show_menu(step_id="channel_groups", menu_options=menu)

    async def async_step_add_cgroup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("channels"):
                errors["channels"] = "channels_required"
            else:
                options = self._options
                groups = list(options.get(CONF_CHANNEL_GROUPS, []))
                groups.append(
                    {
                        "id": uuid4().hex[:8],
                        "name": user_input["name"],
                        "channels": [int(n) for n in user_input["channels"]],
                    }
                )
                options[CONF_CHANNEL_GROUPS] = groups
                return self._save(options)

        return self.async_show_form(
            step_id="add_cgroup", data_schema=self._cgroup_schema(), errors=errors
        )

    async def async_step_edit_cgroup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_cgroup_id = user_input["group"]
            return await self.async_step_edit_cgroup_form()

        return self.async_show_form(
            step_id="edit_cgroup",
            data_schema=vol.Schema(
                {
                    vol.Required("group"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._cgroup_select_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_cgroup_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self._options
        groups = list(options.get(CONF_CHANNEL_GROUPS, []))
        current = next(
            (g for g in groups if g["id"] == self._edit_cgroup_id), None
        )
        if current is None:
            return await self.async_step_channel_groups()

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("channels"):
                errors["channels"] = "channels_required"
            else:
                updated = {
                    "id": current["id"],
                    "name": user_input["name"],
                    "channels": [int(n) for n in user_input["channels"]],
                }
                options[CONF_CHANNEL_GROUPS] = [
                    updated if g["id"] == current["id"] else g for g in groups
                ]
                return self._save(options)

        return self.async_show_form(
            step_id="edit_cgroup_form",
            data_schema=self.add_suggested_values_to_schema(
                self._cgroup_schema(),
                {
                    "name": current["name"],
                    "channels": [str(n) for n in current["channels"]],
                },
            ),
            errors=errors,
        )

    async def async_step_remove_cgroup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = self._options
            removed = set(user_input.get("groups", []))
            options[CONF_CHANNEL_GROUPS] = [
                g
                for g in options.get(CONF_CHANNEL_GROUPS, [])
                if g["id"] not in removed
            ]
            return self._save(options)

        return self.async_show_form(
            step_id="remove_cgroup",
            data_schema=vol.Schema(
                {
                    vol.Required("groups", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._cgroup_select_options(),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # --- tests --------------------------------------------------------------

    async def async_step_tests(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="tests", menu_options=["flash_test"])

    async def async_step_flash_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Flash the appliance's front-panel LEDs — a visible comms test."""
        try:
            await self.config_entry.runtime_data.symetrix.async_flash()
        except SymetrixError:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_tests()

    # --- connection ---------------------------------------------------------

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = vol.Schema(
            {**CONNECTION_SCHEMA.schema, **MUSISELECT_SCHEMA.schema}
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            skip_test = user_input.pop("skip_test", False)
            if skip_test or await _async_try_connect(
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

    @property
    def _levels(self) -> list[int]:
        return self._options.get(CONF_LEVELS, DEFAULT_LEVELS)

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
                channels.append(_channel_from_input(user_input, self._levels))
                options[CONF_CHANNELS] = channels
                return self._save(options)

        return self.async_show_form(
            step_id="add_channel",
            data_schema=_channel_schema(self._levels),
            errors=errors,
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
                updated = _channel_from_input(user_input, self._levels)
                options[CONF_CHANNELS] = [
                    updated if c["number"] == current["number"] else c
                    for c in channels
                ]
                if new_number != current["number"]:
                    # keep groups pointing at the renumbered channel
                    for key in (CONF_PAUSE_GROUPS, CONF_CHANNEL_GROUPS):
                        options[key] = [
                            {
                                **group,
                                "channels": [
                                    new_number if n == current["number"] else n
                                    for n in group["channels"]
                                ],
                            }
                            for group in options.get(key, [])
                        ]
                return self._save(options)

        return self.async_show_form(
            step_id="edit_channel_form",
            data_schema=self.add_suggested_values_to_schema(
                _channel_schema(self._levels),
                _channel_suggested(current, self._levels),
            ),
            errors=errors,
            description_placeholders={"name": current["name"]},
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
            for key in (CONF_PAUSE_GROUPS, CONF_CHANNEL_GROUPS):
                options[key] = [
                    {
                        **group,
                        "channels": [
                            n for n in group["channels"] if n not in removed
                        ],
                    }
                    for group in options.get(key, [])
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
                vol.Optional("preset"): _optional_int(1, 150),
                vol.Optional("command"): vol.Any(
                    None, TextSelector(TextSelectorConfig(multiline=True))
                ),
                vol.Optional("duration"): _optional_int(1, 3600, "s"),
                vol.Optional("return_preset"): _optional_int(1, 150),
            }
        )

    @staticmethod
    def _event_from_input(event_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
        event: dict[str, Any] = {
            "id": event_id,
            "name": user_input["name"],
        }
        if user_input.get("preset"):
            event["preset"] = user_input["preset"]
        command, choices = _parse_commands(user_input.get("command") or "")
        if choices:
            event["options"] = choices
        elif command:
            event["command"] = command
        if user_input.get("duration"):
            event["duration"] = user_input["duration"]
        if user_input.get("return_preset"):
            event["return_preset"] = user_input["return_preset"]
        return event

    @staticmethod
    def _event_options_valid(user_input: dict[str, Any]) -> bool:
        """False only when the command text is present but malformed."""
        try:
            _parse_commands(user_input.get("command") or "")
        except InvalidCommands:
            return False
        return True

    @staticmethod
    def _event_action_valid(user_input: dict[str, Any]) -> bool:
        """An event must do something: load a preset or send a command."""
        try:
            command, choices = _parse_commands(user_input.get("command") or "")
        except InvalidCommands:
            return False
        return bool(user_input.get("preset") or command or choices)

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
            if not self._event_options_valid(user_input):
                errors["command"] = "invalid_event_options"
            elif not self._event_action_valid(user_input):
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
            if not self._event_options_valid(user_input):
                errors["command"] = "invalid_event_options"
            elif not self._event_action_valid(user_input):
                errors["base"] = "event_action_required"
            else:
                updated = self._event_from_input(current["id"], user_input)
                options[CONF_EVENT_BUTTONS] = [
                    updated if e["id"] == current["id"] else e for e in events
                ]
                return self._save(options)

        suggested = dict(current)
        suggested["command"] = _format_commands(current)
        return self.async_show_form(
            step_id="edit_event_form",
            data_schema=self.add_suggested_values_to_schema(
                self._event_schema(), suggested
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
