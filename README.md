# Modulaser → WLED Bridge

Sync [WLED](https://kno.wled.ge/) LED devices to a [Modulaser](https://modulaser.app/) laser show. Colors, effects, blackout, and BPM from the laser software drive your LED strips in real time.

## Features

- **Two color sources**, selected at startup:
  - **OSC base color** — follows Modulaser's OSC feedback: clip/layer color, global color override, group Colorize, opacity, strobe/chase effects, blackout, and BPM.
  - **NDI frame sync** — samples Modulaser's rendered NDI video output so gradients and animated colors stay in sync, with three mappings:
    - `gradient` — per-LED colors streamed over DDP; motion flows along the strip
    - `dominant` — the whole segment follows the frame's strongest color
    - `zones` — the frame is split into vertical zones, one per segment
- **Network discovery** — continuously finds WLED devices via mDNS (with subnet-sweep fallback) and prompts you to pick one or more at each start; the list keeps growing while shown, with `r` to refresh and `s` to sweep the whole subnet
- **Multi-device, multi-segment** — drive several WLED controllers at once, with per-device mappings from Modulaser output groups to WLED segments
- **Strobe & chase sync** — enabling Modulaser's strobe/chase effects switches the mapped WLED segments to matching effects at a synced rate
- **BPM sync** — Modulaser's tempo retimes running WLED effects
- **Live commands** — switch color source or NDI mapping while running (`mode osc`, `mode ndi gradient`), no restart needed
- **Beat flash** — optional brightness pulses synced to Modulaser's BPM, works in every mode (`beat on|off`)
- **Show-over watchdog** — when Modulaser goes quiet for N minutes, every device returns to normal lighting (startup state or a configured WLED preset)
- **Fill-light mode** — inverts the relationship: LEDs stay dark while the lasers project (so they never wash out the beams) and come on with the last laser color when the lasers go dark (`fill on|off`)
- **State restore** — on exit, every WLED device is put back exactly how it was found
- **Auto-reconnect** — devices that drop offline are retried, and missed updates are re-queued

## Requirements

- **Python 3.9+** (tested on 3.9, 3.11, 3.12).
- **OS:** Linux, macOS, and Windows. mDNS discovery relies on `zeroconf`; if it
  is unavailable the bridge still runs using configured/remembered device IPs.
- **WLED:** any recent build with the JSON API and (for `gradient` mode) DDP
  realtime enabled.
- **NDI mode** additionally needs `ndi-python` + `numpy`. `ndi-python` is
  sparsely maintained and lacks wheels for some Python versions/platforms; if
  it will not install, use the OSC color source instead.

## Install

```
pip install python-osc requests zeroconf pyyaml
```

For NDI frame sync, additionally (or use the packaged extra below):

```
pip install ndi-python numpy
```

Or install from a checkout using the packaged metadata:

```
pip install .            # core
pip install .[ndi]       # core + NDI frame sync
pip install .[test]      # core + pytest/numpy for running the tests
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
| `--debug` | Verbose logging: unhandled OSC and all WLED updates |
| `--dry-run` | Log intended WLED/DDP writes instead of sending; no hardware or discovery needed (handy for demos/CI) |
| `--headless` | Service mode: no interactive prompt, uses the saved selection and color source, runs until Ctrl+C/SIGTERM |
| `--log-level debug\|info\|warning\|error` | Logging verbosity (default `info`) |
| `--log-file PATH` | Also write logs to a file (for unattended shows) |

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

Other keys: `beat_flash` / `beat_flash_depth` (beat pulse on/off and how deep it dips), `watchdog_minutes` (restore normal lighting after the show goes quiet; `0` disables), per-device `idle_preset` (recall a WLED preset number instead of the startup snapshot when the watchdog fires), `fill_light` / `fill_threshold` (fill-light mode; the threshold is the fraction of frame pixels that must be lit for the lasers to count as projecting — laser-darkness detection uses NDI frames, while in OSC mode fill keys off the blackout toggle), and `sweep_cidr` (override the auto-detected subnet used by the `s` sweep, e.g. `192.168.1.0/24`, for multi-homed hosts or non-/24 LANs).

Invalid values produce a clear `Config error in wled_bridge.yaml: <key>: ...` message naming the offending key rather than a traceback.

## Ports

| Port | Protocol | Direction | Purpose |
| ---- | -------- | --------- | ------- |
| `9000/udp` | OSC | Modulaser → bridge | Color/effect/BPM feedback (`listen_port`) |
| `8000/udp` | OSC | bridge → Modulaser | One `/refresh` on start (`modulaser_osc_in_port`) |
| `80/tcp` | HTTP | bridge → WLED | JSON state API (`/json/state`, `/json/info`) |
| `4048/udp` | DDP | bridge → WLED | Realtime per-LED streaming (gradient mode) |
| `5353/udp` | mDNS | both | WLED device discovery |

Make sure your firewall allows inbound `9000/udp` on the machine running the
bridge and outbound HTTP/DDP/mDNS to the LED devices.

## Troubleshooting

- **No devices found:** press `s` to sweep the subnet, or type a device IP
  directly. On a multi-homed host or a non-/24 LAN set `sweep_cidr` in
  `wled_bridge.yaml`. You can also add devices under `devices:` and run with
  `--auto`/`--headless` to skip discovery entirely.
- **NDI source not appearing:** enable **NDI output** in Modulaser's output
  settings; the bridge logs `NDI source lost … reconnecting` and retries with
  backoff when a source drops. `status` shows `ndi=connected`/`no-signal`.
- **`ndi-python` won't install:** it lacks wheels on some Python
  versions/platforms — use the OSC color source, which needs no extra deps.
- **Wrong LED count / truncated gradient:** set `led_count` to match the
  strip; a mismatch logs a one-time DDP warning and is clipped/padded to fit.
- **Firewall / no updates:** confirm inbound `9000/udp` is open and the LED
  devices are reachable over HTTP (`80`) and DDP (`4048`).
- **Try it without hardware:** `--dry-run` logs every intended write so you
  can validate mappings and OSC wiring before a show.

## How it works

Modulaser sends OSC feedback for every parameter change. The bridge listens on UDP, maps color/effect/BPM addresses onto WLED's JSON API (debounced, ~20 Hz), and in NDI mode receives rendered frames, samples them with numpy (ignoring the black background), and streams per-LED colors over DDP at up to 30 fps.

OSC feedback only carries *parameter* values, so colors computed inside Modulaser's node graph (gradients, per-point animation) are invisible to OSC — that's exactly what NDI frame sync is for.

## Development

Run the test suite (pure-logic unit tests: color math, NDI normalization,
sampling, DDP framing, OSC mapping, config validation, client debounce). No
hardware or NDI runtime required:

```
pip install .[test]
pytest
```

CI runs `py_compile` + `pytest` on Python 3.9/3.11/3.12 (see
`.github/workflows/ci.yml`).

## License

MIT
