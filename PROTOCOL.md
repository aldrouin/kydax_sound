# Symetrix Control Protocol for Jupiter — reference

Condensed from the official Symetrix "Control Protocol for Jupiter" document.
This is the protocol the integration speaks to the appliance (a Jupiter 8).

## Transport

- **UDP** (not TCP, not Telnet), port **48630** on the device's IP.
- One command per UDP packet; never split a command across packets.
- ASCII text, terms separated by spaces, terminated with `<CR>` (0x0D).
  A trailing NUL on commands is optional; responses never include one.
- Responses (and pushed data) go to the **source IP:port of the last packet
  the device received** — saved in non-volatile memory across power cycles.
  Until the device has received at least one valid packet, it cannot push.
- Port defaults: Quiet Mode ON (`SQ 1`, terse machine-parsable replies) and
  Echo OFF (`EH 0`). Both persisted in NV memory; leave at defaults.

## Value model

Everything is a "controller number" (1–10000) with a 16-bit position (0–65535).
Controller numbers per Jupiter app come from the Jupiter software:
Select Jupiter App → View External Controllers (HTML export).

- **Faders**: linear in dB. `dB = min + (max − min) × pos / 65535`.
  Standard volume range −72…+12 dB → `dB = −72 + 84 × pos/65535`; pos 0 = OFF.
- **Buttons** (mute/bypass): 0 = off, 65535 = on (a few are inverted, see the
  product appendix). Reads come back quantized to exactly 0 or 65535.
- **Input selectors**: evenly spaced; `pos = (n−1) × 65535 / (N−1)`.
- **Meters** (read-only): `dBu = 72 × pos/65535 − 48` (65535 = +24 dBu/0 dBFS).
- Ratios, frequencies, Q, attack/release/hold: log scale. Pans/delays: linear.

## Commands

| Command | Purpose | Success response |
|---|---|---|
| `CS <ctrl> <pos>` | Set absolute value | `ACK` |
| `CC <ctrl> <0\|1> <amount>` | Decrement (0) / increment (1), clamped | `ACK` |
| `GS <ctrl>` | Get value | `<pos>` (bare number) |
| `GS2 <ctrl>` | Get value, number echoed | `<ctrl> <pos>` |
| `GSB <start> <n>` | Block read, n ≤ 256 | 5-digit zero-padded lines; `-0001` = no such controller |
| `GSB2 <start> <n>` | Block read with numbers | `#00009=32321` per line |
| `GPR D` | Last loaded preset | `PrstD=0007` (0 = none); some firmware NAKs this — treat any reply as proof of connectivity |
| `LP <preset>` | Load preset (doc says 1–150 here, 0–50 under GPR) | `ACK` |
| `FU` | Flash front-panel LEDs (comms test) | `ACK` |
| `SQ <0\|1>` | Quiet mode (keep 1) | `ACK` |
| `EH <0\|1>` | Echo mode (keep 0) | `ACK` |

Any interpreted-but-failed command returns `NAK<CR>` (typically: controller
number doesn't exist). All responses end with `<CR>`.

## Push (unsolicited data)

The device can push controller changes instead of being polled. Pushed lines
use the GSB2 format — `#00007=12321<CR>` — up to 64 lines per packet, sent
each push interval (default 100 ms).

Two gates must both be open:
1. **Global**: `PU 1` / `PU 0`. ON at power-up. Never use `PU` with a range.
2. **Per-controller**: `PUE [lo [hi]]` / `PUD [lo [hi]]` (additive/
   subtractive, multiple ranges OK). All DISABLED at power-up.

| Command | Purpose |
|---|---|
| `PUE [lo [hi]]` | Enable push for controller(s); no args = all 1–10000 |
| `PUD [lo [hi]]` | Disable push for controller(s) |
| `GPU [lo [hi]]` | List push-enabled controllers (`ACK` if none) |
| `GPU 0` | Push settings: `Global=<0/1>` + `lo hi paramThresh meterThresh interval` |
| `PUR [lo [hi]]` | Force push of current values (refresh/sync) — only for already-enabled controllers |
| `PUC [lo [hi]]` | Discard pending unreported changes (issue before PUE to avoid a flood) |
| `PUI <ms>` | Push interval, 20–30000 ms (default 100) |
| `PUT [param [meter]]` | Change thresholds, default 1 each; one arg sets both |

Notes:
- Changes made while push was disabled are reported the moment it's
  re-enabled unless `PUC` is issued first.
- At power-up all values count as "changed", so the first `PUE` immediately
  pushes current values — handy for initial sync (or `PUC` first to suppress).
- Push only flows after the device has received ≥1 packet from us (it needs
  a return address), and it targets whoever sent the last packet — if
  anything else (e.g. another controller) sends a command, pushes redirect
  there. HA's poll/keepalive traffic re-claims the address.

## Site facts (from the old component)

- Device: Jupiter 8, restaurant "Pacini Marché Central", UDP port 48630
  (site IP configured in the integration, not recorded here).
- Zone volume controllers used: 7122, 7128, 7134, 7140, 7146, 7152, 7158,
  7164 (spacing of 6 suggests neighboring controllers per zone strip).
- Old code capped volume at 70% of range and treated pos 0 as mute.
- Volume "scenes" (switchson_0/50/60/70/80/90/100) set each zone to a
  specific dB level from a per-scene table (see old const.py VOLUME_VALUES);
  zone 7164 stayed at −33 dB in every scene above 50.
- Pause switch "SALON_PRIVE" muted zone 7152.
- Presets (from the old "fête" scripts): **LP 1 = special/birthday source
  routing, LP 2 = normal source**. The birthday song window was 45 s
  (script: fete service with preset 1 → 45 s delay → update_preset 2).

## MusiSelect (second appliance, documentation pending)

Known only from the old component — official docs to come:

- UDP, port 2325 (static in practice; site IP configured in the
  integration, not recorded here), ASCII payload, e.g. `PACINI diffSpecial 2`.
  `PACINI` appears to be a site/venue prefix. No `<CR>` was appended and
  responses were never read (fire-and-forget), so the response format and
  reliability behavior are unknown.
- The zones always play through the MusiSelect; `diffSpecial` selects which
  music program it outputs: `PACINI diffSpecial 1` = French,
  `PACINI diffSpecial 2` = English.
- "Fête" flow and ordering: load the Symetrix preset (`LP <n>`) to switch the
  Jupiter's source routing, **wait a delay** for the source change to settle,
  then send `diffSpecial <lang>` to change the music program. Exact delay and
  the "back to normal" sequence to be confirmed from the latest deployed code.
- The old code here may not be the latest deployed version — compare with the
  live HA box before trusting the command list; a stop/resume command likely
  exists too.
