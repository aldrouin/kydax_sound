# Kydax Sound

Custom Home Assistant integration for restaurant audio: a Symetrix Jupiter DSP appliance (volumes, mutes, presets — see `PROTOCOL.md`) and, later, a MusiSelect source device. Sibling of [kydax_light](https://github.com/aldrouin/kydax_light) and built on the same architecture: UI-managed configuration, one hub device, live-reloading options, EN/FR translations.

**Status: functional** — UDP protocol client, config flow with connection test, availability tracking, configurable pause groups (mute + lock a set of channels, with visible state) and volume scenes (per-channel dB levels applied together, skipping paused channels). A Jupiter simulator for testing without the appliance lives in `tools/symetrix_sim.js`.

## Install (HACS custom repository)

HACS → ⋮ → Custom repositories → `https://github.com/aldrouin/kydax_symetrix`, type **Integration** → download, restart HA, then Settings → Devices & Services → Add integration → Kydax Sound.
