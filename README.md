# Kydax Sound

Custom Home Assistant integration for restaurant audio: a Symetrix Jupiter DSP appliance (volumes, mutes, presets — see `PROTOCOL.md`) and, later, a MusiSelect source device. Sibling of [kydax_light](https://github.com/aldrouin/kydax_light) and built on the same architecture: UI-managed configuration, one hub device, live-reloading options, EN/FR translations.

**Status: functional** — setup wizard (Symetrix connection with test, MusiSelect address, channels one form at a time). Each channel is calibrated with two values — its volume at 50% and at 100% — and every other percentage is interpolated linearly in dB. A configurable list of percentage levels (default 0/50/60/70/80/90/100) generates one button per level plus an active-level select. Also: pause groups (mute + lock channels, state survives restarts), event switches (Symetrix preset → delay → MusiSelect command → duration → return preset, with a countdown sensor; blocked while paused), reset-to-default-volumes button, and a diagnostic flash-LEDs button. Simulators for both appliances live in `tools/`.

## Install (HACS custom repository)

HACS → ⋮ → Custom repositories → `https://github.com/aldrouin/kydax_sound`, type **Integration** → download, restart HA, then Settings → Devices & Services → Add integration → Kydax Sound.
