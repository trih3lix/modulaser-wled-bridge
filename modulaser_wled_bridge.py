"""
Modulaser -> WLED bridge.

Syncs WLED LED devices to a Modulaser laser show. Two color sources, chosen
at startup:

  OSC base color (default)
    Follows Modulaser's OSC feedback: clip/layer color, global color
    override, group Colorize, opacity, strobe/chase effects, blackout, BPM.

  NDI frame sync
    Samples Modulaser's rendered NDI video output, so gradients and animated
    colors stay in sync. Three mappings:
      gradient  - per-LED colors streamed over DDP; motion flows along the strip
      dominant  - whole segment follows the frame's strongest color
      zones     - frame split into vertical zones, one per global segment
    Needs: pip install ndi-python numpy, and NDI output enabled in Modulaser.

Run:  python modulaser_wled_bridge.py [--debug] [--auto] [--source osc|ndi]
                                      [--mode gradient|dominant|zones]
  Every start scans the network and asks which WLED device(s) to drive
  (Enter = same as last time), then which color source to use. --auto skips
  the device prompt; --source/--mode skip the color source prompts.
  --debug prints unhandled OSC and all WLED updates.

Config lives in wled_bridge.yaml next to this script. Per-device mappings:

  devices:
    192.168.0.10:
      name: MagWLED-1
      led_count: 59                    # used by gradient mode (auto-detected)
      global_color_segments: [0]       # segments that follow the global/clip color
      groups:                          # Modulaser output group -> WLED segment(s)
        0: 0
        1: [1, 2]

While running, type commands to switch live (no restart needed):
  mode osc | mode ndi [gradient|dominant|zones] | beat on|off | fill on|off |
  status | quit

fill_light mode inverts the relationship: LEDs stay dark while the lasers
project and come on with the last laser color when the lasers go dark, so
the LEDs never wash out the beams. Toggle live with 'fill on|off'.

Extra config keys: beat_flash (BPM-synced brightness pulses),
beat_flash_depth (how deep the pulse dips, 0-1), watchdog_minutes (restore
normal lighting after the show goes quiet; 0 disables). Per-device
idle_preset recalls a WLED preset number instead of the startup snapshot.
fill_light / fill_threshold control fill-light mode (threshold = fraction of
frame pixels lit above which the lasers count as projecting; NDI mode only,
OSC mode keys off the blackout toggle).

On exit (Ctrl+C or 'quit') each WLED device is restored to its prior state.
"""

import argparse
import colorsys
import concurrent.futures
import ipaddress
import socket
import threading
import time
from pathlib import Path

import requests
import yaml
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

try:
    import numpy as np
except ImportError:
    np = None

try:
    from zeroconf import ServiceBrowser, Zeroconf
except ImportError:  # pragma: no cover - optional at import time
    # mDNS discovery is unavailable (e.g. zeroconf not installed / failed to
    # load). The app still runs with configured/remembered device IPs; only
    # the auto-discovery UI is affected.
    ServiceBrowser = Zeroconf = None

CONFIG_PATH = Path(__file__).with_name("wled_bridge.yaml")

DEFAULT_CONFIG = {
    "listen_ip": "0.0.0.0",
    "listen_port": 9000,
    "modulaser_ip": "127.0.0.1",
    "modulaser_osc_in_port": 8000,
    "max_rate_hz": 20,
    "restore_on_exit": True,
    "bpm_sync": True,
    "effects": {"solid": 0, "strobe": 23, "chase": 28},
    "color_source": "osc",
    "ndi_mode": "gradient",
    "ndi_fps": 30,
    "ndi_source": None,
    "beat_flash": False,
    "beat_flash_depth": 0.5,
    "fill_light": False,
    "fill_threshold": 0.002,
    "watchdog_minutes": 10,
    "sweep_cidr": None,  # override the auto-detected subnet for the 's' sweep
    "devices": {},
    "selected": [],
}

DEFAULT_DEVICE_MAPPING = {"global_color_segments": [0], "groups": {0: 0}}

# BPM range the bridge maps onto WLED's 0-255 effect-speed (sx) byte.
BPM_MIN, BPM_MAX = 20.0, 300.0


def bpm_to_sx(bpm):
    """Map a BPM (BPM_MIN..BPM_MAX) to a WLED effect-speed byte (0..255)."""
    span = BPM_MAX - BPM_MIN
    return round(max(0.0, min(1.0, (bpm - BPM_MIN) / span)) * 255)


def osc_to_bpm(value):
    """Interpret Modulaser's OSC BPM feedback: a 0..1 value is normalized into
    BPM_MIN..BPM_MAX; a value > 1 is treated as an absolute BPM."""
    if value <= 1.0:
        return BPM_MIN + value * (BPM_MAX - BPM_MIN)
    return float(value)


# ---- Config ------------------------------------------------------------------

class ConfigError(Exception):
    """Raised for an invalid wled_bridge.yaml with an operator-friendly message."""


def _as_int(value, key):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected a whole number, got {value!r}")


def _as_number(value, key):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected a number, got {value!r}")


def validate_config(cfg):
    """Type-check and coerce known keys, raising ConfigError with the offending
    key named so a YAML typo produces a clear message, not a deep traceback."""
    cfg["listen_port"] = _as_int(cfg["listen_port"], "listen_port")
    cfg["modulaser_osc_in_port"] = _as_int(
        cfg["modulaser_osc_in_port"], "modulaser_osc_in_port")
    cfg["max_rate_hz"] = _as_number(cfg["max_rate_hz"], "max_rate_hz")
    if cfg["max_rate_hz"] <= 0:
        raise ConfigError("max_rate_hz: must be greater than 0")
    cfg["ndi_fps"] = _as_number(cfg["ndi_fps"], "ndi_fps")
    cfg["watchdog_minutes"] = _as_number(
        cfg.get("watchdog_minutes") or 0, "watchdog_minutes")
    cfg["fill_threshold"] = _as_number(cfg["fill_threshold"], "fill_threshold")

    if cfg["color_source"] not in ("osc", "ndi"):
        raise ConfigError(
            f"color_source: must be 'osc' or 'ndi', got {cfg['color_source']!r}")
    if cfg["ndi_mode"] not in ("gradient", "dominant", "zones"):
        raise ConfigError(
            "ndi_mode: must be gradient/dominant/zones, "
            f"got {cfg['ndi_mode']!r}")

    if not isinstance(cfg.get("effects"), dict):
        raise ConfigError("effects: must be a mapping of name -> effect id")
    for name in ("solid", "strobe", "chase"):
        if name not in cfg["effects"]:
            raise ConfigError(f"effects.{name}: missing required effect id")
        cfg["effects"][name] = _as_int(cfg["effects"][name], f"effects.{name}")

    devices = cfg.get("devices")
    if not isinstance(devices, dict):
        raise ConfigError("devices: must be a mapping of ip -> settings")
    for ip, mapping in devices.items():
        if not isinstance(mapping, dict):
            raise ConfigError(f"devices.{ip}: must be a mapping of settings")
        if "led_count" in mapping:
            mapping["led_count"] = _as_int(
                mapping["led_count"], f"devices.{ip}.led_count")
        segs = mapping.get("global_color_segments", [])
        if not isinstance(segs, list):
            raise ConfigError(
                f"devices.{ip}.global_color_segments: must be a list of "
                f"segment ids, got {segs!r}")
        groups = mapping.get("groups")
        if groups is not None and not isinstance(groups, dict):
            raise ConfigError(
                f"devices.{ip}.groups: must be a mapping of group -> "
                f"segment(s), got {groups!r}")
    if not isinstance(cfg.get("selected"), list):
        raise ConfigError("selected: must be a list of device IPs")
    if cfg.get("sweep_cidr"):
        try:
            ipaddress.ip_network(cfg["sweep_cidr"], strict=False)
        except ValueError as e:
            raise ConfigError(f"sweep_cidr: {e}")
    return cfg


def load_config():
    user = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                user = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"could not parse {CONFIG_PATH.name}: {e}")
        if not isinstance(user, dict):
            raise ConfigError(
                f"{CONFIG_PATH.name}: top level must be a mapping of settings")
    # migrate v2 single-device config
    if user.get("wled_ip"):
        ip = user.pop("wled_ip")
        devices = user.setdefault("devices", {})
        devices.setdefault(ip, {
            "name": "WLED",
            "global_color_segments": user.pop("global_color_segments", [0]),
            "groups": user.pop("groups", {0: 0}),
        })
        user.setdefault("selected", [ip])
    cfg = {**DEFAULT_CONFIG, **user}
    cfg["effects"] = {**DEFAULT_CONFIG["effects"], **(user.get("effects") or {})}
    return validate_config(cfg)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"Config saved to {CONFIG_PATH}")


# ---- WLED discovery ----------------------------------------------------------

def probe_wled(ip, timeout=0.5):
    try:
        r = requests.get(f"http://{ip}/json/info", timeout=timeout)
        info = r.json()
        if info.get("brand", "").lower() == "wled" or "leds" in info:
            return {"ip": ip, "name": info.get("name", "WLED"),
                    "version": info.get("ver", "?"),
                    "leds": info.get("leds", {}).get("count", "?")}
    except (requests.RequestException, ValueError):
        pass
    return None


class MdnsDiscovery:
    """Continuously discovers WLED devices over mDNS in the background.

    Probing happens off the Zeroconf callback thread so a slow device never
    stalls discovery of the others. Call snapshot() any time for the devices
    found so far; the browser keeps running until close()."""

    def __init__(self):
        if Zeroconf is None:
            raise RuntimeError(
                "mDNS discovery needs the 'zeroconf' package (pip install "
                "zeroconf), or it failed to load. Enter a device IP manually "
                "or list devices under 'devices:' in wled_bridge.yaml.")
        self._found = {}
        self._lock = threading.Lock()
        self._closed = False
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)
        self._zc = Zeroconf()
        discovery = self

        class Listener:
            def add_service(self, zc, type_, name):
                if discovery._closed:
                    return
                info = zc.get_service_info(type_, name, timeout=2000)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    fallback = {"ip": ip, "name": name.split(".")[0],
                                "version": "?", "leds": "?"}
                    discovery._record(ip, fallback)
                    if not discovery._closed:
                        discovery._pool.submit(discovery._probe, ip, fallback)

            def update_service(self, zc, type_, name):
                self.add_service(zc, type_, name)

            def remove_service(self, zc, type_, name):
                pass

        self._browser = ServiceBrowser(self._zc, "_wled._tcp.local.", Listener())

    def _record(self, ip, entry):
        with self._lock:
            # don't downgrade a fully-probed entry back to a fallback
            if ip not in self._found or self._found[ip].get("version") == "?":
                self._found[ip] = entry

    def add_manual(self, entry):
        """Inject a device found outside mDNS (e.g. a subnet sweep) into the
        catalog. Public replacement for reaching into _record."""
        self._record(entry["ip"], entry)

    def _probe(self, ip, fallback):
        if self._closed:
            return
        info = probe_wled(ip, timeout=2)
        self._record(ip, info or fallback)

    def snapshot(self):
        with self._lock:
            return {ip: dict(e) for ip, e in self._found.items()}

    def close(self):
        # Stop accepting new work first so no probe is submitted against a
        # closing Zeroconf, then tear down the browser and the pool.
        self._closed = True
        try:
            self._zc.close()
        finally:
            self._pool.shutdown(wait=False)


def discover_mdns(seconds=5):
    """One-shot discovery (used by non-interactive paths)."""
    d = MdnsDiscovery()
    try:
        time.sleep(seconds)
        return list(d.snapshot().values())
    finally:
        d.close()


def local_subnet(cidr=None):
    """Return the network to sweep. Honors an explicit CIDR (from config);
    otherwise infers the local IP via the default route and assumes /24."""
    if cidr:
        return ipaddress.ip_network(cidr, strict=False)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def discover_subnet(cfg=None):
    cidr = (cfg or {}).get("sweep_cidr")
    try:
        net = local_subnet(cidr)
    except ValueError as e:
        print(f"Invalid sweep_cidr {cidr!r}: {e}")
        return []
    hosts = list(net.hosts())
    if cidr:
        print(f"Sweeping {net} (from sweep_cidr), {len(hosts)} hosts...")
    else:
        print(f"Sweeping {net} (auto-detected; set 'sweep_cidr' in "
              f"wled_bridge.yaml to override), {len(hosts)} hosts...")
    if len(hosts) > 1024:
        print("  That's a large range and may take a while; a narrower "
              "sweep_cidr will be faster.")
    devices = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe_wled, (str(h) for h in hosts)):
            if result:
                devices.append(result)
    return devices


def _merge_remembered(discovered, cfg):
    """Add remembered devices that haven't announced themselves yet."""
    for ip, entry in (cfg.get("devices") or {}).items():
        if ip not in discovered:
            discovered[ip] = probe_wled(ip, timeout=1) or {
                "ip": ip, "name": entry.get("name", "WLED"),
                "version": "?", "leds": "offline?"}
    return discovered


def select_devices(cfg, initial_wait=5):
    """Scan continuously, list devices, and let the user pick one or more.

    Discovery keeps running while the list is shown, so the catalog grows as
    slower devices answer. Press 'r' to refresh, 's' to sweep the subnet.
    Returns a list of IPs."""
    print("Scanning for WLED devices via mDNS "
          f"(listening {initial_wait}s; more may appear)...")
    discovery = MdnsDiscovery()
    try:
        # Poll during the initial wait so an early-answering device shows up
        # fast, but keep listening the full window for stragglers.
        deadline = time.time() + initial_wait
        while time.time() < deadline:
            time.sleep(0.5)
        last = cfg.get("selected") or []

        def current():
            d = _merge_remembered(discovery.snapshot(), cfg)
            return sorted(d.values(), key=lambda x: x["ip"])

        while True:
            devices = current()
            if not devices:
                answer = input("No devices found yet. [r]escan, [s]weep "
                               "subnet, or enter an IP: ").strip().lower()
                if answer in ("r", ""):
                    time.sleep(3)
                    continue
                if answer == "s":
                    for d in discover_subnet(cfg):
                        discovery.add_manual(d)
                    continue
                return [answer]

            print("\nAvailable WLED devices:")
            for i, d in enumerate(devices, 1):
                mark = "  (last used)" if d["ip"] in last else ""
                print(f"  [{i}] {d['name']:<22} {d['ip']:<15} "
                      f"v{d['version']}  {d['leds']} LEDs{mark}")
            print("  [r] rescan / refresh list    [s] sweep whole subnet")

            prompt = "\nSelect devices, e.g. 1 or 1,3 or 'a' for all"
            prompt += " [Enter = last used]: " if last else ": "
            raw = input(prompt).strip().lower()
            if raw == "r":
                print("Still listening...")
                time.sleep(3)
                continue
            if raw == "s":
                print("Sweeping the subnet...")
                for d in discover_subnet(cfg):
                    discovery.add_manual(d)
                continue
            if not raw and last:
                kept = [ip for ip in last if ip in {d["ip"] for d in devices}]
                if kept:
                    return kept
                print("Last-used device(s) not present; pick from the list.")
                continue
            if raw == "a":
                return [d["ip"] for d in devices]
            try:
                idxs = [int(t) for t in raw.replace(",", " ").split()]
            except ValueError:
                idxs = []
            if idxs and all(1 <= i <= len(devices) for i in idxs):
                return [devices[i - 1]["ip"] for i in idxs]
            print("Invalid choice. Use numbers, 'a', 'r', or 's'.")
    finally:
        discovery.close()


def prompt_choice(title, options, last=None):
    """Numbered picker. options is a list of (key, label); returns a key."""
    print(f"\n{title}")
    for i, (key, label) in enumerate(options, 1):
        mark = "  (last used)" if key == last else ""
        print(f"  [{i}] {label}{mark}")
    keys = [k for k, _ in options]
    prompt = "Select [Enter = last used]: " if last in keys else "Select: "
    while True:
        raw = input(prompt).strip()
        if not raw and last in keys:
            return last
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return keys[int(raw) - 1]
        print("Invalid choice.")


# ---- WLED client ---------------------------------------------------------------

class WledClient:
    """Debounced, auto-reconnecting WLED JSON API client."""

    def __init__(self, ip, max_rate_hz, debug=False):
        self.ip = ip
        self.debug = debug
        self.min_interval = 1.0 / max_rate_hz
        self._pending = {}
        self._raw = []                # one-shot full payloads (restore/preset)
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._online = True
        self._shutting_down = False
        self._backoff = 0.0
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def begin_shutdown(self):
        """Freeze the pending queue so no in-flight color updates can clobber
        the final restore. Safe to call from any thread."""
        with self._lock:
            self._shutting_down = True

    def stop(self):
        self._stop.set()
        self._dirty.set()
        self._thread.join(timeout=3)

    def close(self):
        """Release the pooled HTTP connections. Call after any final
        post_state (e.g. the shutdown restore) has been sent."""
        self._session.close()

    def queue(self, top=None, segment=None, seg_fields=None):
        with self._lock:
            if self._shutting_down:
                return
            if top:
                self._pending.update(top)
            if segment is not None and seg_fields:
                segs = self._pending.setdefault("seg", {})
                segs.setdefault(segment, {}).update(seg_fields)
        self._dirty.set()

    def queue_raw(self, payload):
        """Enqueue a one-shot full payload (restore snapshot / preset recall)
        to be sent as-is by the worker thread. Keeps the owning worker the
        sole caller of post_state so no other thread touches the socket."""
        with self._lock:
            if self._shutting_down:
                return
            self._raw.append(dict(payload))
        self._dirty.set()

    def _take_pending(self):
        """Return (raw_payloads, merged_pending_or_None) and clear the queues."""
        with self._lock:
            raws, self._raw = self._raw, []
            if not self._pending:
                return raws, None
            pending, self._pending = self._pending, {}
        segs = pending.pop("seg", None)
        if segs:
            pending["seg"] = [{"id": sid, **fields} for sid, fields in segs.items()]
        return raws, pending

    def _run(self):
        while not self._stop.is_set():
            self._dirty.wait()
            if self._stop.is_set():
                break
            self._dirty.clear()
            raws, payload = self._take_pending()
            for raw in raws:
                self.post_state(raw)
            if payload:
                self.post_state(payload)
            time.sleep(self.min_interval)

    def post_state(self, payload, timeout=1):
        try:
            if self.debug:
                print(f"-> WLED {self.ip}: {payload}")
            self._session.post(f"http://{self.ip}/json/state", json=payload,
                               timeout=timeout)
            with self._lock:
                self._backoff = 0.0
                if not self._online:
                    print(f"WLED {self.ip} back online.")
                    self._online = True
        except requests.RequestException as e:
            with self._lock:
                if self._online:
                    print(f"WLED {self.ip} unreachable ({e}); will keep retrying.")
                    self._online = False
                if self._shutting_down:
                    # Don't requeue during shutdown: the frozen queue must not
                    # be repopulated behind the final restore.
                    return
                segs = {s["id"]: {k: v for k, v in s.items() if k != "id"}
                        for s in payload.pop("seg", [])}
                merged = dict(payload)
                merged.update(self._pending)
                if segs:
                    for sid, fields in (self._pending.get("seg") or {}).items():
                        segs.setdefault(sid, {}).update(fields)
                    merged["seg"] = segs
                self._pending = merged
                self._backoff = min(max(0.5, self._backoff * 2), 8.0)
                backoff = self._backoff
            self._dirty.set()
            # Interruptible: a stop() during a dead-device backoff wakes at once
            # instead of blocking shutdown for the full pause.
            self._stop.wait(backoff)

    def get_state(self, timeout=2):
        try:
            return self._session.get(f"http://{self.ip}/json/state",
                                     timeout=timeout).json()
        except (requests.RequestException, ValueError) as e:
            print(f"Could not read WLED {self.ip} state: {e}")
            return None


def snapshot_for_restore(state):
    if not state:
        return None
    snap = {"on": state.get("on", True), "bri": state.get("bri", 128)}
    segs = []
    for seg in state.get("seg", []):
        if "id" not in seg:
            continue
        segs.append({k: seg[k] for k in ("id", "col", "fx", "sx", "bri", "on")
                     if k in seg})
    if segs:
        snap["seg"] = segs
    return snap


# ---- DDP (realtime pixel streaming) ---------------------------------------------

class DdpSender:
    """Minimal DDP sender for WLED's realtime UDP protocol (port 4048)."""

    MAX_DATA = 1440  # payload bytes per packet (multiple of 3)

    def __init__(self, host, port=4048):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self._warned_len = False
        self._send_failed = False

    def _fit(self, data, led_count):
        """Clip/pad data to led_count*3 bytes, warning once on a mismatch
        (usually a wrong led_count in config vs the real strip length)."""
        if led_count is None:
            return data
        expected = led_count * 3
        if len(data) == expected:
            return data
        if not self._warned_len:
            print(f"DDP {self.addr[0]}: got {len(data)} bytes, expected "
                  f"{expected} ({led_count} LEDs); check led_count. "
                  "Clipping/padding to fit.")
            self._warned_len = True
        if len(data) > expected:
            return data[:expected]
        return data + b"\x00" * (expected - len(data))

    def send(self, data, led_count=None):
        """data: raw RGB bytes (3 per LED). Returns True on success.
        A transient socket error is logged once and swallowed so the caller's
        frame thread stays alive."""
        data = self._fit(data, led_count)
        self.seq = self.seq % 15 + 1
        offset = 0
        try:
            while True:
                chunk = data[offset:offset + self.MAX_DATA]
                last = offset + len(chunk) >= len(data)
                flags = 0x40 | (0x01 if last else 0x00)  # ver 1 | push on final
                header = (bytes([flags, self.seq, 0x01, 0x01])
                          + offset.to_bytes(4, "big")
                          + len(chunk).to_bytes(2, "big"))
                self.sock.sendto(header + chunk, self.addr)
                offset += len(chunk)
                if last:
                    break
        except OSError as e:
            if not self._send_failed:
                print(f"DDP {self.addr[0]} send failed ({e}); will keep trying.")
                self._send_failed = True
            return False
        if self._send_failed:
            print(f"DDP {self.addr[0]} send recovered.")
            self._send_failed = False
        return True


# ---- Frame sampling --------------------------------------------------------------

LUM_THRESHOLD = 20  # 0-255; pixels darker than this count as background


def _lum(rgb):
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _downscale(rgb, max_w=320):
    step = max(1, rgb.shape[1] // max_w)
    return rgb[::step, ::step] if step > 1 else rgb


def sample_gradient(rgb, count):
    """Split the frame into `count` column bins; return (count, 3) uint8 colors,
    each the luminance-weighted average of that bin's lit pixels."""
    rgb = _downscale(rgb)
    w = rgb.shape[1]
    lum = _lum(rgb)
    out = np.zeros((count, 3), dtype=np.uint8)
    edges = np.linspace(0, w, count + 1).astype(int)
    for i in range(count):
        lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
        cols = rgb[:, lo:hi].reshape(-1, 3).astype(np.float32)
        l = lum[:, lo:hi].reshape(-1)
        mask = l > LUM_THRESHOLD
        if mask.any():
            wgt = l[mask]
            out[i] = np.clip(np.rint((cols[mask] * wgt[:, None]).sum(0)
                                     / wgt.sum()), 0, 255)
    return out


def sample_dominant(rgb):
    """Return the frame's strongest color as a (3,) uint8 array.
    Averages only the brightest lit pixels so background doesn't wash it out."""
    rgb = _downscale(rgb)
    lum = _lum(rgb).reshape(-1)
    flat = rgb.reshape(-1, 3).astype(np.float32)
    peak = lum.max() if lum.size else 0.0
    if peak <= LUM_THRESHOLD:
        return np.zeros(3, dtype=np.uint8)
    mask = lum >= max(LUM_THRESHOLD, 0.7 * peak)
    wgt = lum[mask]
    return np.clip(np.rint((flat[mask] * wgt[:, None]).sum(0) / wgt.sum()),
                   0, 255).astype(np.uint8)


def sample_zones(rgb, k):
    """Split the frame into k vertical zones; return (k, 3) uint8 dominant
    colors, one per zone."""
    rgb = _downscale(rgb)
    w = rgb.shape[1]
    edges = np.linspace(0, w, k + 1).astype(int)
    out = np.zeros((k, 3), dtype=np.uint8)
    for i in range(k):
        lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
        out[i] = sample_dominant(rgb[:, lo:hi])
    return out


def ndi_to_rgb(data, w, h):
    """Normalize a raw NDI BGRA buffer to an (h, w, 3) RGB array.

    NDI frames sometimes arrive flat or with line-stride padding instead of
    a clean (h, w, 4) shape; this handles all layouts."""
    arr = np.asarray(data)
    if arr.ndim != 3 or arr.shape[0] != h or arr.shape[1] != w:
        flat = arr.reshape(h, -1)          # rows, possibly padded
        arr = flat[:, :w * 4].reshape(h, w, 4)
    return arr[..., 2::-1]


# ---- NDI ---------------------------------------------------------------------------

class NdiSource:
    """Receives video frames from Modulaser's NDI output."""

    def __init__(self, preferred=None, timeout=6.0):
        try:
            import NDIlib as ndi
        except ImportError:
            raise RuntimeError(
                "NDI mode needs extra packages:  pip install ndi-python numpy")
        self.ndi = ndi
        if not ndi.initialize():
            raise RuntimeError("Could not initialize the NDI runtime.")

        find = ndi.find_create_v2()
        deadline = time.time() + timeout
        sources = []
        print("Looking for NDI sources...")
        while time.time() < deadline:
            ndi.find_wait_for_sources(find, 1000)
            sources = ndi.find_get_current_sources(find)
            if sources:
                time.sleep(1)  # catch late announcers
                sources = ndi.find_get_current_sources(find)
                break
        if not sources:
            ndi.find_destroy(find)
            raise RuntimeError("No NDI sources found. Enable NDI output in "
                               "Modulaser's output settings and try again.")

        names = [s.ndi_name for s in sources]
        pick = None
        if preferred in names:
            pick = names.index(preferred)
        else:
            matches = [i for i, n in enumerate(names)
                       if "modulaser" in n.lower()]
            if len(matches) == 1:
                pick = matches[0]
            elif len(names) == 1:
                pick = 0
        if pick is None:
            print("\nNDI sources:")
            for i, n in enumerate(names, 1):
                print(f"  [{i}] {n}")
            while True:
                raw = input(f"Select source [1-{len(names)}]: ").strip()
                if raw.isdigit() and 1 <= int(raw) <= len(names):
                    pick = int(raw) - 1
                    break
                print("Invalid choice.")

        self.name = names[pick]
        print(f"NDI: receiving from '{self.name}'")
        settings = ndi.RecvCreateV3()
        settings.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        self.recv = ndi.recv_create_v3(settings)
        ndi.recv_connect(self.recv, sources[pick])
        ndi.find_destroy(find)

    def get_frame(self, timeout=0.5):
        """Return an (h, w, 3) RGB uint8 array, or None if no frame arrived."""
        ndi = self.ndi
        if self.recv is None:
            time.sleep(timeout)
            return None
        t, v, _, _ = ndi.recv_capture_v2(self.recv, int(timeout * 1000))
        if t == ndi.FRAME_TYPE_VIDEO:
            data = np.copy(v.data)
            w, h = v.xres, v.yres
            ndi.recv_free_video_v2(self.recv, v)
            return ndi_to_rgb(data, w, h)
        return None

    def reconnect(self, timeout=5.0):
        """Best-effort: tear down and re-open the receiver for the same source
        name (handles Modulaser toggling its NDI output off/on). Returns True
        once the receiver is reconnected."""
        ndi = self.ndi
        try:
            if self.recv is not None:
                ndi.recv_destroy(self.recv)
        except Exception:
            pass
        self.recv = None
        try:
            find = ndi.find_create_v2()
            deadline = time.time() + timeout
            chosen = None
            while time.time() < deadline and chosen is None:
                ndi.find_wait_for_sources(find, 1000)
                for s in ndi.find_get_current_sources(find):
                    if s.ndi_name == self.name:
                        chosen = s
                        break
            if chosen is None:
                ndi.find_destroy(find)
                return False
            settings = ndi.RecvCreateV3()
            settings.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
            self.recv = ndi.recv_create_v3(settings)
            ndi.recv_connect(self.recv, chosen)
            ndi.find_destroy(find)
            return True
        except Exception:
            return False

    def close(self):
        try:
            self.ndi.recv_destroy(self.recv)
            self.ndi.destroy()
        except Exception:
            pass


class FrameSync(threading.Thread):
    """Pulls frames from a source and pushes sampled colors to the devices."""

    def __init__(self, frame_source, devices, mode, fps, bridge,
                 ddp_port=4048, debug=False, fill_threshold=0.002):
        super().__init__(daemon=True)
        self.source = frame_source
        self.devices = devices
        self.mode = mode
        self.interval = 1.0 / max(1, fps)
        self.bridge = bridge
        self.debug = debug
        self.fill_threshold = fill_threshold
        self._fill_dark = False
        self._held = None       # last dominant color while lasers were lit
        self._last_fill = None  # last fill output (avoid JSON re-sends)
        self._stop_evt = threading.Event()
        self._next_log = 0.0
        self._ddp = {}
        if mode == "gradient":
            self._ddp = {d.ip: DdpSender(d.ip.split(":")[0], ddp_port)
                         for d in devices}

    def stop(self):
        self._stop_evt.set()

    STALL_TIMEOUT = 3.0   # seconds without a frame before declaring source lost

    def run(self):
        next_t = 0.0
        last_frame = time.time()
        lost = False
        next_reconnect = 0.0
        reconnect_backoff = 2.0
        while not self._stop_evt.is_set():
            frame = self.source.get_frame(timeout=0.5)
            now = time.time()
            if frame is None:
                if not lost and now - last_frame > self.STALL_TIMEOUT:
                    lost = True
                    self.bridge.ndi_connected = False
                    next_reconnect = now
                    print("[NDI] source lost (no frames); reconnecting...")
                if lost and now >= next_reconnect \
                        and hasattr(self.source, "reconnect"):
                    if self.source.reconnect():
                        print("[NDI] receiver reconnected; awaiting frames.")
                    else:
                        reconnect_backoff = min(reconnect_backoff * 2, 30.0)
                    next_reconnect = now + reconnect_backoff
                continue
            last_frame = now
            if lost:
                lost = False
                reconnect_backoff = 2.0
                print("[NDI] source recovered.")
            self.bridge.ndi_connected = True
            self.bridge.last_activity = now
            if now < next_t:
                continue
            next_t = now + self.interval
            if self.bridge.fill_enabled:
                self._run_fill(frame)
                continue
            if self.bridge.blackout:
                frame = None
            if self.debug and frame is not None and now >= self._next_log:
                self._next_log = now + 2.0
                lit = float((_lum(frame) > LUM_THRESHOLD).mean()) * 100.0
                head = sample_gradient(frame, 5).tolist()
                print(f"[frame] shape={frame.shape} lit={lit:.1f}% "
                      f"5-bin sample={head}")
            for d in self.devices:
                if self.mode == "gradient":
                    if frame is None:
                        colors = np.zeros((d.led_count, 3), dtype=np.uint8)
                    else:
                        colors = sample_gradient(frame, d.led_count)
                    self._ddp[d.ip].send(colors.tobytes(), d.led_count)
                elif self.mode == "dominant":
                    rgb = ([0, 0, 0] if frame is None
                           else [int(c) for c in sample_dominant(frame)])
                    for seg in d.global_segs:
                        d.client.queue(segment=seg, seg_fields={"col": [rgb]})
                else:  # zones
                    k = max(1, len(d.global_segs))
                    cols = (np.zeros((k, 3), dtype=np.uint8) if frame is None
                            else sample_zones(frame, k))
                    for seg, rgb in zip(d.global_segs, cols):
                        d.client.queue(
                            segment=seg,
                            seg_fields={"col": [[int(c) for c in rgb]]})


    def _run_fill(self, frame):
        """Fill-light mode: LEDs come on (with the last laser color) only
        while the lasers are dark."""
        lit = 0.0
        if frame is not None and not self.bridge.blackout:
            small = _downscale(frame)
            lit = float((_lum(small) > LUM_THRESHOLD).mean())
            if lit >= self.fill_threshold:
                self._held = [int(c) for c in sample_dominant(frame)]
        if frame is None or self.bridge.blackout:
            self._fill_dark = True
        elif lit >= self.fill_threshold:
            self._fill_dark = False
        elif lit <= self.fill_threshold * 0.5:
            self._fill_dark = True
        # else: inside the hysteresis band, keep the previous state
        out = (self._held or [255, 255, 255]) if self._fill_dark else [0, 0, 0]
        changed = out != self._last_fill
        self._last_fill = list(out)
        for d in self.devices:
            if self.mode == "gradient":
                # DDP realtime times out if we stop streaming, so always send
                colors = np.tile(np.array(out, dtype=np.uint8),
                                 (d.led_count, 1))
                self._ddp[d.ip].send(colors.tobytes(), d.led_count)
            elif changed:
                for seg in d.global_segs:
                    d.client.queue(segment=seg,
                                   seg_fields={"col": [list(out)]})


# ---- Devices -------------------------------------------------------------------

class Device:
    """A WLED device plus its Modulaser mapping."""

    def __init__(self, ip, mapping, client):
        self.ip = ip
        self.name = mapping.get("name", "WLED")
        self.client = client
        self.led_count = int(mapping.get("led_count", 30))
        self.idle_preset = mapping.get("idle_preset")
        self.global_segs = [int(s) for s in
                            mapping.get("global_color_segments", [0])]
        groups = mapping.get("groups") or {}
        self.group_seg = {}
        for g, segs in groups.items():
            if not isinstance(segs, list):
                segs = [segs]
            self.group_seg[int(g)] = [int(s) for s in segs]


# ---- Bridge --------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg, devices, debug=False):
        self.cfg = cfg
        self.devices = devices
        self.debug = debug
        self.fx = cfg["effects"]
        self.lock = threading.Lock()
        self.global_hsl = {"hue": 0.0, "saturation": 1.0, "lightness": 0.5}
        self.group_color = {}      # group -> {"hsl": {...}, "rgb": None|{...}}
        self.active_fx = {}        # (client, seg) -> current effect id
        self.bpm = 120.0
        self.blackout = False
        self.frame_sync = False    # True = NDI owns colors; OSC colors ignored
        self.ndi_connected = False  # True while NDI frames are arriving
        self.last_activity = time.time()
        self.idle = False          # set by the watchdog when the show goes quiet
        self.beat_enabled = False
        self.base_bri = 128
        self.fill_enabled = False  # LEDs only when lasers are dark

    # -- helpers --

    @staticmethod
    def _norm(value):
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _hsl_rgb(hsl):
        r, g, b = colorsys.hls_to_rgb(hsl["hue"], hsl["lightness"],
                                      hsl["saturation"])  # HLS arg order!
        return [round(r * 255), round(g * 255), round(b * 255)]

    @staticmethod
    def _canon_key(key):
        return {"sat": "saturation", "light": "lightness",
                "val": "level", "value": "level",
                "r": "red", "g": "green", "b": "blue"}.get(key, key)

    def _global_targets(self):
        for d in self.devices:
            for seg in d.global_segs:
                yield d.client, seg

    def _group_targets(self, group):
        for d in self.devices:
            for seg in d.group_seg.get(group, []):
                yield d.client, seg

    def _bpm_sx(self):
        return bpm_to_sx(self.bpm)

    # -- OSC handlers --

    def on_global_color(self, address, *args):
        self.last_activity = time.time()
        if not args:
            return
        key = self._canon_key(address.rsplit("/", 1)[-1])
        value = self._norm(args[0])
        with self.lock:
            if key == "level":
                bri = max(1, round(value * 255))
                self.base_bri = bri
                for d in self.devices:
                    d.client.queue(top={"bri": bri})
                return
            if self.frame_sync or key not in self.global_hsl:
                return
            self.global_hsl[key] = value
            rgb = self._hsl_rgb(self.global_hsl)
        if self.fill_enabled and not self.blackout:
            return  # lasers projecting: LEDs stay dark, color is just remembered
        for client, seg in self._global_targets():
            client.queue(segment=seg, seg_fields={"col": [rgb]})

    def on_blackout(self, address, *args):
        self.last_activity = time.time()
        if not args:
            return
        self.blackout = float(args[0]) >= 0.5
        if self.fill_enabled and not self.frame_sync:
            self._apply_fill_osc()
            return
        for d in self.devices:
            d.client.queue(top={"on": not self.blackout})

    def _apply_fill_osc(self):
        """OSC fill mode: lasers dark (blackout) -> show the held color;
        lasers projecting -> LEDs off (black)."""
        with self.lock:
            rgb = self._hsl_rgb(self.global_hsl) if self.blackout else [0, 0, 0]
        for d in self.devices:
            d.client.queue(top={"on": True})
        for client, seg in self._global_targets():
            client.queue(segment=seg, seg_fields={"col": [rgb]})

    def on_group_opacity(self, address, *args):
        self.last_activity = time.time()
        if not args:
            return
        group = self._group_from(address)
        bri = round(self._norm(args[0]) * 255)
        for client, seg in self._group_targets(group):
            client.queue(segment=seg, seg_fields={"bri": bri})

    def on_group_fx(self, address, *args):
        """Generic handler for /group/{g}/fx/{slot}/{key}.

        {slot} may be a preset short name (colorize, strobe, chase) or a
        numeric effect slot. Color-like keys are treated as Colorize
        regardless of how the slot is addressed; rate/enabled keys on the
        strobe and chase presets drive the matching WLED effect.
        """
        self.last_activity = time.time()
        parts = address.split("/")
        if len(parts) < 6 or not args:
            return
        group = self._group_from(address)
        slot = parts[4]
        key = self._canon_key(parts[5])

        if slot == "strobe":
            self._rate_effect(group, key, args[0], "strobe")
        elif slot == "chase":
            self._rate_effect(group, key, args[0], "chase")
        elif slot == "colorize" or slot.isdigit():
            self._colorize(group, key, args[0])

    def _colorize(self, group, key, value):
        if self.frame_sync or key in ("enabled", "on"):
            return
        with self.lock:
            col = self.group_color.setdefault(group, {
                "hsl": {"hue": 0.0, "saturation": 1.0, "lightness": 0.5},
                "rgb": None})
            if key in col["hsl"]:
                col["hsl"][key] = self._norm(value)
                col["rgb"] = None           # HSL keys are authoritative now
                rgb = self._hsl_rgb(col["hsl"])
            elif key in ("red", "green", "blue"):
                if col["rgb"] is None:
                    col["rgb"] = {"red": 255, "green": 255, "blue": 255}
                col["rgb"][key] = round(self._norm(value) * 255)
                rgb = [col["rgb"]["red"], col["rgb"]["green"], col["rgb"]["blue"]]
            else:
                return  # not a color key (e.g. some other slot parameter)
        if self.fill_enabled and not self.blackout:
            return
        for client, seg in self._group_targets(group):
            client.queue(segment=seg, seg_fields={"col": [rgb]})

    def _rate_effect(self, group, key, value, name):
        fx_id = self.fx[name]
        if key in ("enabled", "on"):
            new_fx = fx_id if float(value) >= 0.5 else self.fx["solid"]
            for client, seg in self._group_targets(group):
                with self.lock:
                    self.active_fx[(client, seg)] = new_fx
                client.queue(segment=seg, seg_fields={"fx": new_fx})
        elif key in ("rate", "speed", "frequency", "speedhz"):
            sx = round(self._norm(value) * 255)
            for client, seg in self._group_targets(group):
                client.queue(segment=seg, seg_fields={"sx": sx})

    def on_bpm(self, address, *args):
        self.last_activity = time.time()
        if not args or not self.cfg["bpm_sync"]:
            return
        bpm = osc_to_bpm(float(args[0]))
        with self.lock:
            self.bpm = bpm
            sx = self._bpm_sx()
            targets = [(client, seg) for (client, seg), fx
                       in self.active_fx.items()
                       if fx not in (self.fx["solid"], self.fx["strobe"])]
        for client, seg in targets:
            client.queue(segment=seg, seg_fields={"sx": sx})

    @staticmethod
    def _group_from(address):
        part = address.split("/")[2]
        return int(part) if part.isdigit() else -1

    # -- wiring --

    def build_dispatcher(self):
        d = Dispatcher()
        for p in ("hue", "saturation", "sat", "lightness", "light",
                  "level", "val", "value"):
            d.map(f"/output/color/{p}", self.on_global_color)
        # Clip-level color (sent when you change color in the clip editor).
        # "value"/"val" excluded: for clips that's HSV value, not brightness.
        for p in ("hue", "saturation", "sat", "lightness", "light"):
            d.map(f"/clip/color/{p}", self.on_global_color)
        d.map("/output/blackout", self.on_blackout)
        d.map("/group/*/opacity", self.on_group_opacity)
        d.map("/group/*/fx/*/*", self.on_group_fx)
        d.map("/bpm/value", self.on_bpm)
        def _unmapped(addr, *a):
            self.last_activity = time.time()
            if self.debug:
                print(f"OSC (unmapped): {addr} {a}")

        d.set_default_handler(_unmapped)
        return d


class BeatFlash(threading.Thread):
    """Beat-synced master brightness pulses derived from Modulaser's BPM."""

    ATTACK = 0.12  # seconds the peak holds before dipping to the rest level

    def __init__(self, bridge, devices, depth=0.5):
        super().__init__(daemon=True)
        self.bridge = bridge
        self.devices = devices
        self.depth = max(0.1, min(0.9, float(depth)))
        self._stop_evt = threading.Event()
        self._was_on = False

    def stop(self):
        self._stop_evt.set()

    def _set_bri(self, bri):
        for d in self.devices:
            d.client.queue(top={"bri": int(bri)})

    def run(self):
        while not self._stop_evt.is_set():
            active = self.bridge.beat_enabled and not self.bridge.idle
            if not active:
                if self._was_on:
                    self._set_bri(self.bridge.base_bri)  # leave brightness clean
                    self._was_on = False
                time.sleep(0.2)
                continue
            self._was_on = True
            period = 60.0 / max(BPM_MIN, min(BPM_MAX, self.bridge.bpm))
            base = max(1, int(self.bridge.base_bri))
            attack = min(self.ATTACK, period * 0.4)
            self._set_bri(base)                              # beat hit
            time.sleep(attack)
            self._set_bri(max(1, round(base * (1.0 - self.depth))))
            if self._stop_evt.wait(max(0.01, period - attack)):
                break


class Watchdog(threading.Thread):
    """Restores normal lighting after the show goes quiet."""

    def __init__(self, bridge, devices, cfg, restore_map, check_interval=2.0):
        super().__init__(daemon=True)
        self.bridge = bridge
        self.devices = devices
        self.cfg = cfg
        self.restore_map = restore_map  # client -> startup snapshot
        self.check_interval = check_interval
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.wait(self.check_interval):
            timeout = float(self.cfg.get("watchdog_minutes") or 0) * 60.0
            if timeout <= 0:
                continue
            quiet = time.time() - self.bridge.last_activity
            if self.bridge.idle:
                if quiet < timeout:
                    self.bridge.idle = False
                    print("\n[watchdog] Modulaser is back; resuming.")
            elif quiet >= timeout:
                self.bridge.idle = True
                print(f"\n[watchdog] No Modulaser activity for "
                      f"{self.cfg['watchdog_minutes']} min; restoring lighting.")
                for d in self.devices:
                    if d.idle_preset is not None:
                        d.client.queue_raw({"ps": int(d.idle_preset)})
                    elif d.client in self.restore_map:
                        d.client.queue_raw(self.restore_map[d.client])


HELP_TEXT = """Live commands:
  mode osc                            follow OSC base color
  mode ndi [gradient|dominant|zones]  sample NDI frames
  beat on|off                         beat-synced brightness pulses
  fill on|off                         LEDs only while lasers are dark
  status                              current source, BPM, devices
  help                                this list
  quit                                exit and restore WLED state"""


class Controller:
    """Runtime control: switch color sources and toggles without restarting."""

    def __init__(self, cfg, bridge, devices, debug=False):
        self.cfg = cfg
        self.bridge = bridge
        self.devices = devices
        self.debug = debug
        self.framesync = None
        self.ndi_src = None

    def set_source(self, source, mode=None):
        mode = mode or self.cfg.get("ndi_mode") or "gradient"
        if self.framesync:
            self.framesync.stop()
            self.framesync.join(timeout=2)
            self.framesync = None
        self.bridge.frame_sync = False
        if source == "ndi":
            if np is None:
                print("numpy is not installed (pip install ndi-python numpy); "
                      "staying on OSC colors.")
                return False
            if self.ndi_src is None:
                try:
                    self.ndi_src = NdiSource(preferred=self.cfg.get("ndi_source"))
                    self.cfg["ndi_source"] = self.ndi_src.name
                except RuntimeError as e:
                    print(f"{e}\nStaying on OSC colors.")
                    return False
            self.bridge.frame_sync = True
            self.framesync = FrameSync(
                self.ndi_src, self.devices, mode,
                self.cfg.get("ndi_fps", 30), self.bridge, debug=self.debug,
                fill_threshold=self.cfg.get("fill_threshold", 0.002))
            self.framesync.start()
            print(f"Color source: NDI/{mode}")
        else:
            print("Color source: OSC base color")
        self.cfg["color_source"] = source
        self.cfg["ndi_mode"] = mode
        save_config(self.cfg)
        return True

    def handle_command(self, line):
        """Returns True when the app should quit."""
        parts = line.split()
        if not parts:
            return False
        cmd, args = parts[0], parts[1:]
        if cmd in ("quit", "exit", "q"):
            return True
        if cmd == "help":
            print(HELP_TEXT)
        elif cmd == "status":
            src = f"ndi/{self.framesync.mode}" if self.framesync else "osc"
            ndi_state = ""
            if self.framesync:
                ndi_state = ("  ndi=connected" if self.bridge.ndi_connected
                             else "  ndi=no-signal")
            print(f"source={src}  bpm={self.bridge.bpm:.0f}  "
                  f"beat={'on' if self.bridge.beat_enabled else 'off'}  "
                  f"fill={'on' if self.bridge.fill_enabled else 'off'}  "
                  f"idle={self.bridge.idle}{ndi_state}")
            for d in self.devices:
                print(f"  {d.name} ({d.ip}): global segs {d.global_segs}, "
                      f"{d.led_count} LEDs")
        elif cmd == "mode":
            if args and args[0] == "osc":
                self.set_source("osc")
            elif args and args[0] == "ndi" and (
                    len(args) < 2
                    or args[1] in ("gradient", "dominant", "zones")):
                self.set_source("ndi", args[1] if len(args) > 1 else None)
            else:
                print("usage: mode osc | mode ndi [gradient|dominant|zones]")
        elif cmd == "beat":
            if args and args[0] in ("on", "off"):
                self.bridge.beat_enabled = args[0] == "on"
                self.cfg["beat_flash"] = self.bridge.beat_enabled
                save_config(self.cfg)
                print(f"Beat flash {args[0]}")
            else:
                print("usage: beat on|off")
        elif cmd == "fill":
            if args and args[0] in ("on", "off"):
                self.bridge.fill_enabled = args[0] == "on"
                self.cfg["fill_light"] = self.bridge.fill_enabled
                save_config(self.cfg)
                if not self.bridge.frame_sync:
                    if self.bridge.fill_enabled:
                        self.bridge._apply_fill_osc()
                    else:
                        # back to normal: resend the current color
                        self.bridge.on_global_color("/output/color/hue",
                                                    self.bridge.global_hsl["hue"])
                print(f"Fill light {args[0]}"
                      + (" (LEDs only while lasers are dark)"
                         if args[0] == "on" else ""))
            else:
                print("usage: fill on|off")
        else:
            print("Unknown command. Type 'help'.")
        return False


# ---- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Modulaser -> WLED bridge")
    parser.add_argument("--auto", action="store_true",
                        help="skip the device prompt and reuse the last selection")
    parser.add_argument("--source", choices=["osc", "ndi"],
                        help="color source (skips the prompt)")
    parser.add_argument("--mode", choices=["gradient", "dominant", "zones"],
                        help="NDI frame mapping (skips the prompt)")
    parser.add_argument("--debug", action="store_true",
                        help="print unhandled OSC messages and all WLED updates")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Config error in {CONFIG_PATH.name}: {e}")
        raise SystemExit(2)

    if args.auto and cfg.get("selected"):
        ips = cfg["selected"]
        print(f"--auto: using last selection: {', '.join(ips)}")
    else:
        ips = select_devices(cfg)

    source = args.source
    if not source:
        source = prompt_choice("Color source:", [
            ("osc", "OSC base color (clip/layer color, Colorize, effects)"),
            ("ndi", "NDI frame sync (gradients & animated colors)"),
        ], last=cfg.get("color_source"))
    mode = args.mode or cfg.get("ndi_mode") or "gradient"
    if source == "ndi" and not args.mode:
        mode = prompt_choice("Frame mapping:", [
            ("gradient", "Per-LED gradient (colors flow along the strip, DDP)"),
            ("dominant", "Dominant color (segments follow the strongest color)"),
            ("zones", "Zones (frame split across the global segments)"),
        ], last=cfg.get("ndi_mode"))

    # ensure every selected device has a mapping entry, then remember choices
    for ip in ips:
        entry = cfg["devices"].setdefault(ip, {})
        info = probe_wled(ip, timeout=1)
        entry.setdefault("name", (info or {}).get("name", "WLED"))
        if info and isinstance(info.get("leds"), int):
            entry.setdefault("led_count", info["leds"])
        entry.setdefault("global_color_segments",
                         list(DEFAULT_DEVICE_MAPPING["global_color_segments"]))
        entry.setdefault("groups", dict(DEFAULT_DEVICE_MAPPING["groups"]))
    cfg["selected"] = ips
    cfg["color_source"] = source
    cfg["ndi_mode"] = mode
    save_config(cfg)

    devices, restores = [], []
    for ip in ips:
        client = WledClient(ip, cfg["max_rate_hz"], debug=args.debug)
        if cfg["restore_on_exit"]:
            snap = snapshot_for_restore(client.get_state())
            if snap:
                restores.append((client, snap))
        client.start()
        devices.append(Device(ip, cfg["devices"][ip], client))

    bridge = Bridge(cfg, devices, debug=args.debug)
    bridge.beat_enabled = bool(cfg.get("beat_flash", False))
    bridge.fill_enabled = bool(cfg.get("fill_light", False))
    if restores:
        bridge.base_bri = restores[0][1].get("bri", 128)

    controller = Controller(cfg, bridge, devices, debug=args.debug)
    if source == "ndi":
        controller.set_source("ndi", mode)

    beat = BeatFlash(bridge, devices, depth=cfg.get("beat_flash_depth", 0.5))
    beat.start()
    watchdog = Watchdog(bridge, devices, cfg, dict(restores))
    watchdog.start()

    server = BlockingOSCUDPServer(
        (cfg["listen_ip"], cfg["listen_port"]), bridge.build_dispatcher())
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        SimpleUDPClient(cfg["modulaser_ip"],
                        cfg["modulaser_osc_in_port"]).send_message("/refresh", [])
    except OSError as e:
        print(f"Could not send /refresh to Modulaser: {e}")

    print(f"\nBridging Modulaser ({cfg['listen_ip']}:{cfg['listen_port']}) "
          f"-> {len(devices)} WLED device(s)"
          + (f", color source: NDI/{mode}" if controller.framesync else
             ", color source: OSC"))
    for d in devices:
        mapped = ", ".join(f"group {g} -> seg {s}"
                           for g, s in sorted(d.group_seg.items()))
        print(f"  {d.name} ({d.ip}): global color -> segments "
              f"{d.global_segs}; {mapped or 'no group mappings'}")
    if bridge.beat_enabled:
        print("Beat flash is ON")
    if bridge.fill_enabled:
        print("Fill light is ON (LEDs only while lasers are dark)")
    print("Type 'help' for live commands (mode / beat / status / quit). "
          "Ctrl+C also quits"
          + (" (WLED state will be restored)." if restores else "."))

    try:
        while True:
            try:
                line = input("> ").strip().lower()
            except EOFError:
                # no interactive stdin (e.g. running as a service): idle here
                time.sleep(1)
                continue
            if controller.handle_command(line):
                break
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down...")
        beat.stop()
        watchdog.stop()
        if controller.framesync:
            controller.framesync.stop()
        if controller.ndi_src:
            controller.ndi_src.close()
        # Freeze every device's pending queue first so no late color update can
        # slip in behind the restore, then stop the worker threads. After the
        # workers have joined, the main thread is the sole caller of post_state,
        # so the restore writes are race-free.
        for d in devices:
            d.client.begin_shutdown()
        for d in devices:
            d.client.stop()
        for client, snap in restores:
            print(f"Restoring {client.ip}...")
            client.post_state(snap, timeout=3)
        for d in devices:
            d.client.close()


if __name__ == "__main__":
    main()
