"""Config and options flows for Kydax Sound.

Initial setup is a wizard: Symetrix connection (tested) -> MusiSelect
address (optional) -> channels, one form each, with their volume calibration
(dB at 50% and at 100%; other percentages are interpolated linearly in dB).
Everything remains editable afterwards through the options flow.
"""

from __future__ import annotations

import json
import os
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
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.http import StaticPathConfig
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    FileSelector,
    FileSelectorConfig,
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
    CONF_LABELS,
    CONF_LANGUAGES,
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
    LABEL_EVENT_END,
    LABEL_LANGUAGE,
    LABEL_VOLUME_LEVEL,
    LABEL_VOLUME_SELECT,
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
    CONF_LANGUAGES,
)
DEFAULT_CONFIG_FILE = "kydax_sound.json"
# exports live under www/ (also reachable at /local/... after a restart) and
# are served immediately from our own route
DOWNLOAD_DIR = "www"
_STATIC_URL = "/kydax_sound_files"
_STATIC_REGISTERED = "kydax_sound_static_registered"


async def _async_download_url(hass, directory: str, name: str) -> str:
    """A URL the browser can fetch the export from, right away.

    Home Assistant only serves /config/www at /local when that folder
    already existed at startup, so a first export would 404 until a
    restart. Registering our own static route avoids that entirely.
    """
    if not hass.data.get(_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_STATIC_URL, directory, False)]
        )
        hass.data[_STATIC_REGISTERED] = True
    return f"{_STATIC_URL}/{name}"


def _strip_comments(value: Any) -> Any:
    """Drop the readable annotations added on export (keys starting with _)."""
    if isinstance(value, dict):
        return {
            key: _strip_comments(item)
            for key, item in value.items()
            if not key.startswith("_")
        }
    if isinstance(value, list):
        return [_strip_comments(item) for item in value]
    return value


def _entity_names(hass, entry_id: str) -> dict[str, str]:
    """Friendly name (and entity id) of every entity this entry created,
    keyed by the unique_id suffix that identifies it."""
    names: dict[str, str] = {}
    if hass is None or entry_id is None:
        return names
    for reg_entry in er.async_entries_for_config_entry(
        er.async_get(hass), entry_id
    ):
        state = hass.states.get(reg_entry.entity_id)
        friendly = (
            state.name
            if state
            else (reg_entry.name or reg_entry.original_name or reg_entry.entity_id)
        )
        suffix = reg_entry.unique_id.removeprefix(f"{entry_id}_")
        names[suffix] = f"{friendly} ({reg_entry.entity_id})"
    return names


def _export_payload(
    options: dict[str, Any], hass=None, entry_id: str | None = None
) -> dict[str, Any]:
    """The portable configuration, annotated so it reads without guesswork.

    Numbers and ids are opaque on their own, so every item also carries the
    friendly name of the entity it created in Home Assistant. Everything
    prefixed with _ is a comment and is ignored when the file is imported.
    """
    names = {
        channel["number"]: channel.get("name", "?")
        for channel in options.get(CONF_CHANNELS, [])
    }
    entities = _entity_names(hass, entry_id)

    def _label(number: int) -> str:
        return f"{names.get(number, 'unknown channel')} ({number})"

    data: dict[str, Any] = {}
    for key in PORTABLE_KEYS:
        if key not in options:
            continue
        value = options[key]
        if key == CONF_CHANNELS:
            value = [
                _annotate(
                    channel,
                    entities.get(f"channel_{channel.get('number')}"),
                    _volumes=_volume_comment(channel),
                )
                for channel in value
            ]
        elif key == CONF_CHANNEL_GROUPS:
            value = [
                _annotate(
                    group,
                    entities.get(f"group_{group.get('id')}"),
                    _channels=[_label(n) for n in group.get("channels", [])],
                )
                for group in value
            ]
        elif key == CONF_PAUSE_GROUPS:
            value = [
                _annotate(
                    group,
                    entities.get(f"pause_{group.get('id')}"),
                    _channels=[_label(n) for n in group.get("channels", [])],
                )
                for group in value
            ]
        elif key == CONF_EVENT_BUTTONS:
            value = [
                _annotate(event, entities.get(f"event_{event.get('id')}"))
                for event in value
            ]
        elif key == CONF_LEVELS:
            data["_levels"] = [
                entities.get(f"level_{level}", f"{level}%") for level in value
            ]
        elif key == CONF_LANGUAGES and entities.get("language"):
            data["_languages"] = entities["language"]
        data[key] = value
    return {
        "_comment": (
            "Kydax Sound configuration. Lines starting with _ are comments "
            "naming the Home Assistant entities behind each item; they are "
            "ignored on import. Appliance addresses are not included."
        ),
        "kydax_sound": data,
    }


def _annotate(item: dict[str, Any], entity: str | None, **comments: Any) -> dict:
    """Copy a configuration item with its entity name and extra comments."""
    annotated = dict(item)
    if entity:
        annotated["_entity"] = entity
    annotated.update(comments)
    return annotated


def _volume_comment(channel: dict[str, Any]) -> str:
    """A human-readable summary of a channel's level table."""
    table = channel_level_table(channel)
    if not table:
        return "no volumes configured"
    return ", ".join(f"{level}% = {table[level]} dB" for level in sorted(table))


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
        self._edit_choice_index: int | None = None
        self._edit_language_index: int | None = None

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
                "languages",
                "labels",
                "backup",
                "tests",
            ],
        )

    # --- entity labels --------------------------------------------------------

    async def async_step_labels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename the entities whose label comes from a translation."""
        keys = (
            LABEL_VOLUME_LEVEL,
            LABEL_VOLUME_SELECT,
            LABEL_LANGUAGE,
            LABEL_EVENT_END,
        )
        if user_input is not None:
            options = self._options
            options[CONF_LABELS] = {
                key: (user_input.get(key) or "").strip()
                for key in keys
                if (user_input.get(key) or "").strip()
            }
            return self._save(options)

        schema = vol.Schema({vol.Optional(key): TextSelector() for key in keys})
        return self.async_show_form(
            step_id="labels",
            data_schema=self.add_suggested_values_to_schema(
                schema, self._options.get(CONF_LABELS, {})
            ),
        )

    # --- MusiSelect programs (languages) --------------------------------------

    def _language_select_options(self) -> list[SelectOptionDict]:
        return [
            SelectOptionDict(value=str(index), label=language["label"])
            for index, language in enumerate(self._options.get(CONF_LANGUAGES, []))
        ]

    def _language_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("label"): TextSelector(),
                vol.Required("command"): TextSelector(),
            }
        )

    def _save_languages(self, languages: list[dict]) -> ConfigFlowResult:
        options = self._options
        options[CONF_LANGUAGES] = languages
        return self._save(options)

    async def async_step_languages(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_language"]
        if self._options.get(CONF_LANGUAGES):
            menu += ["edit_language", "remove_language"]
        return self.async_show_menu(step_id="languages", menu_options=menu)

    async def async_step_add_language(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            languages = list(self._options.get(CONF_LANGUAGES, []))
            languages.append(dict(user_input))
            return self._save_languages(languages)
        return self.async_show_form(
            step_id="add_language", data_schema=self._language_schema()
        )

    async def async_step_edit_language(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_language_index = int(user_input["language"])
            return await self.async_step_edit_language_form()
        return self.async_show_form(
            step_id="edit_language",
            data_schema=vol.Schema(
                {
                    vol.Required("language"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._language_select_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_language_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        languages = list(self._options.get(CONF_LANGUAGES, []))
        index = self._edit_language_index
        if index is None or not 0 <= index < len(languages):
            return await self.async_step_languages()
        if user_input is not None:
            languages[index] = dict(user_input)
            return self._save_languages(languages)
        return self.async_show_form(
            step_id="edit_language_form",
            data_schema=self.add_suggested_values_to_schema(
                self._language_schema(), languages[index]
            ),
        )

    async def async_step_remove_language(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            removed = {int(index) for index in user_input.get("languages", [])}
            languages = [
                language
                for index, language in enumerate(self._options.get(CONF_LANGUAGES, []))
                if index not in removed
            ]
            return self._save_languages(languages)
        return self.async_show_form(
            step_id="remove_language",
            data_schema=vol.Schema(
                {
                    vol.Required("languages", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._language_select_options(),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
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
        """Write the configuration where a browser can download it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["path"].strip() or DEFAULT_CONFIG_FILE
            directory = self.hass.config.path(DOWNLOAD_DIR)
            path = self.hass.config.path(DOWNLOAD_DIR, name)
            payload = _export_payload(
                self._options, self.hass, self.config_entry.entry_id
            )

            def _write() -> None:
                os.makedirs(directory, exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)

            try:
                await self.hass.async_add_executor_job(_write)
                url = await _async_download_url(self.hass, directory, name)
            except OSError:
                errors["path"] = "write_failed"
            else:
                return self.async_abort(
                    reason="exported",
                    description_placeholders={"url": url, "path": path},
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
        """Upload a file and replace channels, levels, groups and events."""
        errors: dict[str, str] = {}
        if user_input is not None:
            def _read() -> Any:
                with process_uploaded_file(self.hass, user_input["file"]) as path:
                    with open(path, encoding="utf-8") as handle:
                        return json.load(handle)

            try:
                data = await self.hass.async_add_executor_job(_read)
            except (OSError, ValueError, KeyError):
                errors["file"] = "invalid_file"
            else:
                payload = _strip_comments(
                    data.get("kydax_sound", data) if isinstance(data, dict) else data
                )
                problem = _validate_payload(payload)
                if problem:
                    errors["file"] = problem
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
                    vol.Required("file"): FileSelector(
                        FileSelectorConfig(accept=".json,application/json")
                    )
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
        """The event's own settings; the choices are managed separately."""
        return vol.Schema(
            {
                vol.Required("name"): TextSelector(),
                vol.Optional("preset"): _optional_int(1, 150),
                vol.Optional("command"): vol.Any(None, TextSelector()),
                vol.Optional("duration"): _optional_int(1, 3600, "s"),
                vol.Optional("return_preset"): _optional_int(1, 150),
            }
        )

    @staticmethod
    def _event_from_input(
        event_id: str, user_input: dict[str, Any], existing: dict | None = None
    ) -> dict[str, Any]:
        """Build an event, preserving any choices it already has."""
        event: dict[str, Any] = {"id": event_id, "name": user_input["name"]}
        if user_input.get("preset"):
            event["preset"] = user_input["preset"]
        command = (user_input.get("command") or "").strip()
        if command:
            event["command"] = command
        if user_input.get("duration"):
            event["duration"] = user_input["duration"]
        if user_input.get("return_preset"):
            event["return_preset"] = user_input["return_preset"]
        for key in ("options", "preset_options"):
            if existing and existing.get(key):
                event[key] = existing[key]
        return event

    def _current_event(self) -> dict | None:
        return next(
            (
                e
                for e in self._options.get(CONF_EVENT_BUTTONS, [])
                if e["id"] == self._edit_event_id
            ),
            None,
        )

    def _save_event(self, updated: dict) -> ConfigFlowResult:
        options = self._options
        options[CONF_EVENT_BUTTONS] = [
            updated if e["id"] == updated["id"] else e
            for e in options.get(CONF_EVENT_BUTTONS, [])
        ]
        return self._save(options)

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
            return await self.async_step_event_menu()

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

    async def async_step_event_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What to change on the selected event."""
        current = self._current_event()
        if current is None:
            return await self.async_step_event_buttons()
        return self.async_show_menu(
            step_id="event_menu",
            menu_options=["edit_event_form", "event_zones"],
            description_placeholders={"name": current["name"]},
        )

    async def async_step_edit_event_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self._current_event()
        if current is None:
            return await self.async_step_event_buttons()

        if user_input is not None:
            return self._save_event(
                self._event_from_input(current["id"], user_input, current)
            )

        return self.async_show_form(
            step_id="edit_event_form",
            data_schema=self.add_suggested_values_to_schema(
                self._event_schema(), current
            ),
        )

    # --- event choices (languages and zones) ----------------------------------

    def _choice_select_options(self, key: str) -> list[SelectOptionDict]:
        current = self._current_event() or {}
        return [
            SelectOptionDict(value=str(index), label=choice["label"])
            for index, choice in enumerate(current.get(key, []))
        ]

    def _choice_schema(self, key: str) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("label"): TextSelector(),
                vol.Required("preset"): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=150, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
            }
        )

    def _save_choices(self, key: str, choices: list[dict]) -> ConfigFlowResult:
        current = dict(self._current_event() or {})
        if choices:
            current[key] = choices
        else:
            current.pop(key, None)
        return self._save_event(current)

    async def _async_choice_menu(self, key: str, step: str) -> ConfigFlowResult:
        current = self._current_event()
        if current is None:
            return await self.async_step_event_buttons()
        prefix = "zone"
        menu = [f"add_{prefix}"]
        if current.get(key):
            menu += [f"edit_{prefix}", f"remove_{prefix}"]
        return self.async_show_menu(
            step_id=step,
            menu_options=menu,
            description_placeholders={"name": current["name"]},
        )

    async def _async_add_choice(
        self, key: str, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        current = self._current_event()
        if current is None:
            return await self.async_step_event_buttons()
        if user_input is not None:
            choices = list(current.get(key, []))
            choices.append(dict(user_input))
            return self._save_choices(key, choices)
        return self.async_show_form(
            step_id=step, data_schema=self._choice_schema(key)
        )

    async def _async_edit_choice(
        self, key: str, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_choice_index = int(user_input["choice"])
            return await self.async_step_edit_zone_form_event()
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required("choice"): SelectSelector(
                        SelectSelectorConfig(
                            options=self._choice_select_options(key),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def _async_edit_choice_form(
        self, key: str, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        current = self._current_event()
        if current is None:
            return await self.async_step_event_buttons()
        choices = list(current.get(key, []))
        index = self._edit_choice_index
        if index is None or not 0 <= index < len(choices):
            return await self._async_choice_menu(key, "event_zones")
        if user_input is not None:
            choices[index] = dict(user_input)
            return self._save_choices(key, choices)
        return self.async_show_form(
            step_id=step,
            data_schema=self.add_suggested_values_to_schema(
                self._choice_schema(key), choices[index]
            ),
        )

    async def _async_remove_choice(
        self, key: str, step: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        current = self._current_event()
        if current is None:
            return await self.async_step_event_buttons()
        if user_input is not None:
            removed = {int(index) for index in user_input.get("choices", [])}
            choices = [
                choice
                for index, choice in enumerate(current.get(key, []))
                if index not in removed
            ]
            return self._save_choices(key, choices)
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required("choices", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._choice_select_options(key),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # zones: a choice of Symetrix presets
    async def async_step_event_zones(self, user_input=None) -> ConfigFlowResult:
        return await self._async_choice_menu("preset_options", "event_zones")

    async def async_step_add_zone(self, user_input=None) -> ConfigFlowResult:
        return await self._async_add_choice(
            "preset_options", "add_zone", user_input
        )

    async def async_step_edit_zone(self, user_input=None) -> ConfigFlowResult:
        return await self._async_edit_choice(
            "preset_options", "edit_zone", user_input
        )

    async def async_step_edit_zone_form_event(self, user_input=None) -> ConfigFlowResult:
        return await self._async_edit_choice_form(
            "preset_options", "edit_zone_form_event", user_input
        )

    async def async_step_remove_zone(self, user_input=None) -> ConfigFlowResult:
        return await self._async_remove_choice(
            "preset_options", "remove_zone", user_input
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