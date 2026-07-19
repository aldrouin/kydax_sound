# Kydax Symetrix

Home Assistant custom integration controlling a Symetrix audio DSP appliance (volumes, mutes, presets) for a restaurant. Sibling of **kydax_light** (`C:\Workspace\kydax_light`, github.com/aldrouin/kydax_light) — that repo is the reference implementation; when in doubt, copy its patterns.

## Purpose / background

Replaces part of the user's old `kydax_dimmer`-era setup (`C:\Workspace\kydax`). Key original pain points to solve: HA must read the appliance's actual state on startup (query the Symetrix over TCP, don't trust last-known HA state), and volume controls must reflect changes made outside HA. Symetrix control protocol is TCP (default port 48631), text commands like `CS <controller> <value>` / `GS <controller>`.

## Architecture pattern (must match kydax_light)

- All config through UI config/options flows; **no hard-coded entity IDs or values in code**. Config lives in `entry.options`; options flow uses `async_show_menu` submenus; every save triggers live reload via `add_update_listener`.
- `coordinator.py` holds a single hub/engine object stored in `entry.runtime_data` (typed `ConfigEntry[Hub]`); it owns all runtime state and timers; entities are thin.
- Entities: `entity.py` base class with `_attr_has_entity_name`, dispatcher-driven updates (`signal_update(entry_id)` from `const.py`), unique_ids `f"{entry.entry_id}_<suffix>"`, one hub device (`DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})`).
- Dynamic entities (created from options): prune stale registry entries in `async_setup_entry` (see kydax_light `_async_prune_stale_pause_entities`).
- Restorable state (toggles the user sets): `RestoreEntity`, restore in `async_added_to_hass`.
- Translations: `strings.json` is the English source; `translations/en.json` is a copy of it; `translations/fr.json` mirrors it in French. **Both languages always, updated in the same commit as the code.**
- NumberSelector gotcha: never pass `unit_of_measurement=None` — only set the key when there is a unit (this crashed kydax_light at import once).

## Environment & verification

- **No Python on this machine** (Windows). Use Node.js for helper scripts (JSON edits etc.).
- Verify before every release inside the real HA image (Docker Desktop may need starting first):
  ```bash
  docker run --rm -v "C:\Workspace\kydax_symetrix\custom_components:/cc:ro" ghcr.io/home-assistant/home-assistant:stable python3 -c "import sys; sys.path.insert(0,'/cc'); import kydax_symetrix.config_flow; print('OK')"
  ```
  Import every module; add small logic asserts the same way kydax_light does.
- CI: `.github/workflows/hassfest.yml` runs hassfest on push.

## Release routine

1. Bump `version` in `manifest.json` (semver: fixes = patch, features = minor).
2. Commit with a descriptive message, push to `main`.
3. `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."` — the tag must match the manifest version.
4. User installs via HACS custom repository (repo is public for that reason). HACS lags new releases — Redownload + version picker forces it.
5. Convention: a release whose notes contain `[critical]` is force-installed at 4 AM by kydax_light's auto-updater even when auto-update is off — reserve for genuinely critical fixes, and only once this integration replicates that feature.

## User preferences (established on kydax_light)

- UI labels in **French and English** via translations; the user's HA runs in French.
- The user tests on their live HA box and pastes log errors; fix, verify in Docker, release, they update via HACS.
- Prefers everything manageable from the UI after initial setup, bulk operations for repetitive config, test/simulation modes (see kydax_light's 10-s fast test sessions), and opt-in (default-off) automation like auto-update.
