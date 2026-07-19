# Kydax Sound

Custom Home Assistant integration for restaurant audio: a Symetrix Jupiter DSP appliance (volumes, mutes, presets — see `PROTOCOL.md`) and, later, a MusiSelect source device. Sibling of [kydax_light](https://github.com/aldrouin/kydax_light) and built on the same architecture: UI-managed configuration, one hub device, live-reloading options, EN/FR translations.

**Status: functional** — setup wizard (Symetrix connection with test, MusiSelect address, channels with default volume %), pause groups (mute + lock channels, state survives restarts), volume scenes (per-channel dB, one click button each + active-scene select), event switches (Symetrix preset → delay → MusiSelect command → duration → return preset, with a countdown sensor; blocked while paused), reset-to-default-volumes button, and a diagnostic flash-LEDs button. Simulators for both appliances live in `tools/`.

## Install (HACS custom repository)

HACS → ⋮ → Custom repositories → `https://github.com/aldrouin/kydax_symetrix`, type **Integration** → download, restart HA, then Settings → Devices & Services → Add integration → Kydax Sound.
