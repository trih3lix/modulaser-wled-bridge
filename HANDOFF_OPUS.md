# 🤖 Opus Handoff — modulaser-wled-bridge

**Date:** 2026-07-08
**Reviewer:** Claude (Opus) — automated code-review / QA pass. No functional changes were made to the code; this document plus a set of GitHub issues capture the findings so the next agent can pick up work with full context.

---

## Project Overview

**What it is:** A single-file, real-time Python bridge that drives [WLED](https://kno.wled.ge/) LED devices to mirror a [Modulaser](https://modulaser.app/) laser show. It is a live-performance tool — the LEDs follow the laser software's colors, effects, blackout, and BPM in real time.

**Stack:** Pure Python 3. One module, `modulaser_wled_bridge.py` (~1270 lines). Runtime deps: `python-osc`, `requests`, `zeroconf`, `pyyaml`; optional `ndi-python` + `numpy` for NDI mode. No packaging (`pyproject.toml`/`setup.py`), no tests, no CI.

**How to run:**
```
pip install python-osc requests zeroconf pyyaml   # + ndi-python numpy for NDI
python modulaser_wled_bridge.py [--auto] [--source osc|ndi] [--mode gradient|dominant|zones] [--debug]
```
Config is auto-created at `wled_bridge.yaml` next to the script (git-ignored).

**Data flow:**
```
Modulaser laser show
   ├── OSC feedback (UDP :9000) ──► Bridge OSC dispatcher ──► HSL/RGB + effect/BPM mapping
   │                                                          ──► WledClient (debounced JSON POST /json/state, ~20 Hz)
   └── NDI video output ──► NdiSource.get_frame ──► FrameSync thread ──► numpy sampling
                                                    ├── gradient: per-LED colors ──► DdpSender (UDP :4048)
                                                    ├── dominant: strongest color ──► WledClient JSON
                                                    └── zones: per-segment colors ──► WledClient JSON
```
Auxiliary threads: `BeatFlash` (BPM-synced brightness pulses), `Watchdog` (restore normal lighting when the show goes quiet), one `WledClient` worker thread per device, a background `MdnsDiscovery` browser + probe pool.

---

## Current State

**What works (by code inspection — not run against live hardware):**
- Module imports and parses cleanly (`python -c "import ast; ast.parse(...)"` passes). Optional numpy/NDI import is guarded.
- OSC → WLED color/effect/BPM mapping, mDNS discovery with subnet-sweep fallback, multi-device/multi-segment mapping, debounced JSON client with reconnect/requeue, DDP realtime streaming, config load/migrate/save, live command loop, state snapshot & restore on exit.
- Reasonable real-time design: per-device worker threads, debounce timer, numpy vectorized sampling with downscale, DDP for high-rate pixel streaming.

**Incomplete / unverified:**
- **No tests at all.** Correctness of OSC address mapping, NDI buffer normalization, color math, and the DDP packet format is unverified against real devices.
- Not runnable in CI without hardware; no mock/dry-run mode.
- NDI code path cannot be exercised without `ndi-python` + a live NDI source.

**Run/test status observed:** static analysis only (no WLED/Modulaser/NDI hardware available in the review environment). Syntax OK; no linter/formatter config present.

---

## Problems & Issues Found

Severity: 🔴 high · 🟠 medium · 🟡 low. Each links the GitHub issue filed for it.

| Sev | Title | Location | Issue |
|-----|-------|----------|-------|
| 🔴 | `requests` calls in the OSC/frame hot path can block the debounce worker and NDI thread | `modulaser_wled_bridge.py:389`, `:1020`, `:411` | #1 |
| 🔴 | No automated tests; correctness of OSC mapping, NDI normalization, DDP framing, and color math is unverified | whole module | #2 |
| 🟠 | `post_state` is called from three threads (worker `_run`, `Watchdog`, shutdown restore) → data race on `_pending`/`_online` and restore can be clobbered | `:374-409`, `:1018-1022`, `:1262-1264` | #3 |
| 🟠 | `DdpSender.send` never checks payload length vs LED count and does not handle `socket.error`; a send failure kills the `FrameSync` thread silently | `:447-461`, `:644-685` | #4 |
| 🟠 | NDI stall = silent busy-loop: `get_frame` timeouts `continue` forever with no reconnect and no "source lost" signal | `:599-608`, `:644-649` | #5 |
| 🟠 | Config is never validated; bad YAML types (e.g. `led_count: "x"`, non-dict `devices`) raise deep in `Device.__init__`/handlers with no friendly message | `:106-123`, `:721-737` | #6 |
| 🟠 | `local_subnet()` assumes a single default route / `/24`; multi-homed or non-/24 LANs sweep the wrong range | `:210-217` | #7 |
| 🟡 | `MdnsDiscovery` accesses `discovery._record` (private) from `select_devices`, and the probe thread pool is shut down with `wait=False` (probes may touch a closed Zeroconf) | `:272`, `:293`, `:193-197` | #8 |
| 🟡 | `WledClient.stop()` joins with a 2 s timeout but a blocked `requests.post` (1 s) + `time.sleep(1)` can exceed it; no socket close | `:350-353`, `:385-409` | #9 |
| 🟡 | No dependency pinning (`requirements.txt` is unversioned); `ndi-python` is unmaintained/wheels-scarce on modern Python — install fragility | `requirements.txt` | #10 |
| 🟡 | Cross-platform: mDNS/DDP/NDI behavior and `input()`-driven loop differ on Windows/macOS/Linux; no service/headless mode beyond the EOF idle hack | `:1240-1247` | #11 |
| 🟡 | Dead/duplicated defaults: `DEFAULT_DEVICE_MAPPING` groups `{0: 0}` re-copied inline; `_bpm_sx` mixing with `on_bpm` scaling is redundant/confusing | `:101`, `:789-790`, `:905-918` | #12 |
| 🟡 | README setup lists `9000` as `listen_port` but frames NDI/DDP ports informally; no troubleshooting, no "no devices found" guidance, no Python-version/OS support matrix | `README.md` | #13 |

**Secrets scan:** ✅ No committed tokens, API keys, or credentials found. `wled_bridge.yaml` (which holds LAN IPs) is correctly git-ignored, and LAN device IPs in the README/docstrings are non-sensitive. The repo is clean on this front.

---

## Resolution Plan

**Phase 1 — Safety & correctness (do first):**
- #3 Serialize all WLED HTTP sends through the single per-device worker thread (route `Watchdog` and shutdown-restore through `queue()` + a drain, or add a dedicated lock/queue). Remove direct `post_state` calls from other threads.
- #1 Confirm no blocking network I/O sits on the OSC dispatcher or NDI frame thread; keep the debounce worker's blocking POST off the frame path (it already is — document/guard it) and cap retry backoff.
- #4 Guard `DdpSender.send` (length check, `try/except OSError`, keep thread alive).
- #5 Add NDI reconnect + "source lost" state and activity signalling so the watchdog behaves correctly when NDI drops.

**Phase 2 — Robustness:**
- #6 Add config schema validation with clear error messages on load.
- #7 Improve subnet detection (enumerate interfaces / honor configured CIDR).
- #8/#9 Clean up discovery lifecycle (public API, ordered shutdown) and client shutdown (close sockets, bound join).

**Phase 3 — Quality & distribution:**
- #2 Introduce tests (pure functions first: `ndi_to_rgb`, `sample_*`, color math, config migrate, DDP header bytes) + a mock WLED server for the client.
- #10 Pin dependencies; add `pyproject.toml` with an optional `[ndi]` extra; add basic CI (lint + tests).
- #13 Improve README (troubleshooting, support matrix, clearer port table).
- #12 Remove dead/redundant code.

---

## Suggested Enhancements / Features

- **Dry-run / simulation mode** (`--dry-run`) that logs intended WLED writes without hardware — enables CI and demos (#14).
- **E1.31 / sACN and DDP-per-segment** output options for larger installs; expose output-protocol choice in config.
- **Structured logging** (`logging` module with levels) instead of `print`, plus optional `--log-file` for unattended shows.
- **Web/OSC status endpoint** so operators can see source/BPM/device health without the console.
- **Packaging**: publish to PyPI as `modulaser-wled-bridge` with a console entry point.
- **Graceful reload** of `wled_bridge.yaml` on change (watch the file) so mappings can be tuned mid-show.

---

## How to Continue (for the next Opus agent)

1. **Read this file and the tracking issue** `🤖 Opus Handoff: Code Review Roadmap & Goals` (it links every issue below into phases).
2. **Start with Phase 1** — the thread-safety of WLED sends (#3) is the highest-leverage correctness fix and unblocks safe reasoning about everything else. Then #4/#5 (frame-path resilience).
3. **Before touching behavior, add tests for the pure functions** (#2): `ndi_to_rgb`, `sample_gradient/dominant/zones`, `Bridge._hsl_rgb`, `load_config` migration, and `DdpSender` header bytes. These are fully unit-testable with numpy and no hardware and will lock in current behavior before refactors.
4. **No hardware in this environment** — you likely won't be able to run against a real WLED/Modulaser rig. Build a mock WLED HTTP server (`/json/info`, `/json/state`) to exercise `WledClient`, and synthesize numpy frames for the samplers.
5. **Keep the single-file ergonomics** the author clearly values (easy to drop on a show laptop) unless packaging is explicitly requested — prefer adding a `tests/` dir and `pyproject.toml` beside it rather than splitting the module prematurely.
6. **Cite `path:line`** in every change/PR and reference the issue numbers so this handoff stays traceable.
