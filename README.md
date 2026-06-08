# Modulaser → WLED Bridge

Sync [WLED](https://kno.wled.ge/) LED devices to a [Modulaser](https://modulaser.app/) laser show. Colors, effects, blackout, and BPM from the laser software drive your LED strips in real time.

## Features

- **Two color sources**, selected at startup:
  - **OSC base color** — follows Modulaser's OSC feedback: clip/layer color, global color override, group Colorize, opacity, strobe/chase effects, blackout, and BPM.
  - **NDI frame sync** — samples Modulaser's rendered NDI video output so gradients and animated colors stay in sync, with three mappings:
    - `gradient` — per-LED colors streamed over DDP; motion flows along the strip
    - `dominant` — the whole segment follows the frame's strongest color
    - `zones` — the frame is split into vertical zones, one per segment
- **Network discovery** — finds WLED devices via mDNS (with subnet-sweep fallback) and prompts you to pick one or more at each start
- **Multi-device, multi-segment** — drive several WLED controllers at once, with per-device mappings from Modulaser output groups to WLED segments
- **Strobe & chase sync** — enabling Modulaser's strobe/chase effects switches the mapped WLED segments to matching effects at a synced rate
- **BPM sync** — Modulaser's tempo retimes running WLED effects
- **Live commands** — switch color source or NDI mapping while running (`mode osc`, `mode ndi gradient`), no restart needed
- **Beat flash** — optional brightness pulses synced to Modulaser's BPM, works in every mode (`beat on|off`)
- **Show-over watchdog** — when Modulaser goes quiet for N minutes, every device returns to normal lighting (startup state or a configured WLED preset)
- **Fill-light mode** — inverts the relationship: LEDs stay dark while the lasers project (so they never wash out the beams) and come on with the last laser color when the lasers go dark (`fill on|off`)
- **State restore** — on exit, every WLED device is put back exactly how it was found
- **Auto-reconnect** — devices that drop offline are retried, and missed updates are re-queued

## Install

```
pip install python-osc requests zeroconf pyyaml
```

For NDI frame sync, additionally:

```
pip install ndi-python numpy
```

## Setup

1. In Modulaser, enable **OSC** and set its OSC *output* target to the machine running this script, port `9000` (the bridge's `listen_port`).
2. For NDI mode, also enable **NDI output** in Modulaser's output settings.
3. Run:

```
python modulaser_wled_bridge.py
```

Pick your WLED device(s) and color source when prompted. Settings are remembered in `wled_bridge.yaml`.

### Options

| Flag | Effect |
| ---- | ------ |
| `--auto` | Skip the device prompt, reuse the last selection |
| `--source osc\|ndi` | Skip the color source prompt |
| `--mode gradient\|dominant\|zones` | Skip the NDI mapping prompt |
| `--debug` | Print unhandled OSC messages and all WLED updates |

### Live commands (while running)

| Command | Effect |
| ------- | ------ |
| `mode osc` | Follow OSC base color |
| `mode ndi [gradient\|dominant\|zones]` | Sample NDI frames |
| `beat on\|off` | BPM-synced brightness pulses |
| `fill on\|off` | LEDs only while lasers are dark |
| `status` | Current source, BPM, devices |
| `quit` | Exit and restore WLED state |

## Configuration

`wled_bridge.yaml` is created next to the script on first run. Per-device mappings:

```yaml
devices:
  192.168.0.10:
    name: MagWLED-1
    led_count: 59                  # used by gradient mode (auto-detected)
    global_color_segments: [0]     # segments that follow the global/clip color
    groups:                        # Modulaser output group -> WLED segment(s)
      0: 0
      1: [1, 2]
```

WLED effect IDs used for strobe/chase sync are configurable under `effects:`.

Other keys: `beat_flash` / `beat_flash_depth` (beat pulse on/off and how deep it dips), `watchdog_minutes` (restore normal lighting after the show goes quiet; `0` disables), per-device `idle_preset` (recall a WLED preset number instead of the startup snapshot when the watchdog fires), and `fill_light` / `fill_threshold` (fill-light mode; the threshold is the fraction of frame pixels that must be lit for the lasers to count as projecting — laser-darkness detection uses NDI frames, while in OSC mode fill keys off the blackout toggle).

## How it works

Modulaser sends OSC feedback for every parameter change. The bridge listens on UDP, maps color/effect/BPM addresses onto WLED's JSON API (debounced, ~20 Hz), and in NDI mode receives rendered frames, samples them with numpy (ignoring the black background), and streams per-LED colors over DDP at up to 30 fps.

OSC feedback only carries *parameter* values, so colors computed inside Modulaser's node graph (gradients, per-point animation) are invisible to OSC — that's exactly what NDI frame sync is for.

## License

MIT
