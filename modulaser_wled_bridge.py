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

On exit (Ctrl+C) the script restores each WLED device to its prior state.
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
from zeroconf import ServiceBrowser, Zeroconf

try:
    import numpy as np
except ImportError:
    np = None

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
    "devices": {},
    "selected": [],
}

DEFAULT_DEVICE_MAPPING = {"global_color_segments": [0], "groups": {0: 0}}


# ---- Config ------------------------------------------------------------------

def load_config():
    user = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user = yaml.safe_load(f) or {}
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
    return cfg


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


def discover_mdns(seconds=4):
    found = {}

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if info and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                found[ip] = probe_wled(ip, timeout=2) or {
                    "ip": ip, "name": name.split(".")[0],
                    "version": "?", "leds": "?"}

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_wled._tcp.local.", Listener())
        time.sleep(seconds)
    finally:
        zc.close()
    return list(found.values())


def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def discover_subnet():
    net = local_subnet()
    print(f"Sweeping {net} (this takes a few seconds)...")
    devices = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe_wled, (str(h) for h in net.hosts())):
            if result:
                devices.append(result)
    return devices


def select_devices(cfg):
    """Scan, list all devices (discovered + remembered), let the user pick
    one or more. Returns a list of IPs."""
    print("Scanning for WLED devices via mDNS...")
    discovered = {d["ip"]: d for d in discover_mdns()}

    # include remembered devices that didn't announce themselves
    for ip, entry in (cfg.get("devices") or {}).items():
        if ip not in discovered:
            discovered[ip] = probe_wled(ip, timeout=1) or {
                "ip": ip, "name": entry.get("name", "WLED"),
                "version": "?", "leds": "offline?"}

    if not discovered:
        answer = input("No devices found. Sweep the local subnet? [Y/n] ")
        if answer.strip().lower() not in ("n", "no"):
            discovered = {d["ip"]: d for d in discover_subnet()}

    if not discovered:
        ip = input("No WLED devices found. Enter an IP manually: ").strip()
        return [ip]

    devices = sorted(discovered.values(), key=lambda d: d["ip"])
    last = [ip for ip in (cfg.get("selected") or []) if ip in discovered]

    print("\nAvailable WLED devices:")
    for i, d in enumerate(devices, 1):
        mark = "  (last used)" if d["ip"] in last else ""
        print(f"  [{i}] {d['name']:<22} {d['ip']:<15} "
              f"v{d['version']}  {d['leds']} LEDs{mark}")

    prompt = "\nSelect devices, e.g. 1 or 1,3 or 'a' for all"
    prompt += " [Enter = last used]: " if last else ": "
    while True:
        raw = input(prompt).strip().lower()
        if not raw and last:
            return last
        if raw == "a":
            return [d["ip"] for d in devices]
        try:
            idxs = [int(t) for t in raw.replace(",", " ").split()]
        except ValueError:
            idxs = []
        if idxs and all(1 <= i <= len(devices) for i in idxs):
            return [devices[i - 1]["ip"] for i in idxs]
        print("Invalid choice.")


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
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._online = True
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._dirty.set()
        self._thread.join(timeout=2)

    def queue(self, top=None, segment=None, seg_fields=None):
        with self._lock:
            if top:
                self._pending.update(top)
            if segment is not None and seg_fields:
                segs = self._pending.setdefault("seg", {})
                segs.setdefault(segment, {}).update(seg_fields)
        self._dirty.set()

    def _take_pending(self):
        with self._lock:
            if not self._pending:
                return None
            pending, self._pending = self._pending, {}
        segs = pending.pop("seg", None)
        if segs:
            pending["seg"] = [{"id": sid, **fields} for sid, fields in segs.items()]
        return pending

    def _run(self):
        while not self._stop.is_set():
            self._dirty.wait()
            if self._stop.is_set():
                break
            self._dirty.clear()
            payload = self._take_pending()
            if payload:
                self.post_state(payload)
            time.sleep(self.min_interval)

    def post_state(self, payload, timeout=1):
        try:
            if self.debug:
                print(f"-> WLED {self.ip}: {payload}")
            requests.post(f"http://{self.ip}/json/state", json=payload,
                          timeout=timeout)
            if not self._online:
                print(f"WLED {self.ip} back online.")
                self._online = True
        except requests.RequestException as e:
            if self._online:
                print(f"WLED {self.ip} unreachable ({e}); will keep retrying.")
                self._online = False
            with self._lock:
                segs = {s["id"]: {k: v for k, v in s.items() if k != "id"}
                        for s in payload.pop("seg", [])}
                merged = dict(payload)
                merged.update(self._pending)
                if segs:
                    for sid, fields in (self._pending.get("seg") or {}).items():
                        segs.setdefault(sid, {}).update(fields)
                    merged["seg"] = segs
                self._pending = merged
            self._dirty.set()
            time.sleep(1)

    def get_state(self, timeout=2):
        try:
            return requests.get(f"http://{self.ip}/json/state",
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

    def send(self, data):
        """data: raw RGB bytes (3 per LED)."""
        self.seq = self.seq % 15 + 1
        offset = 0
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
        t, v, _, _ = ndi.recv_capture_v2(self.recv, int(timeout * 1000))
        if t == ndi.FRAME_TYPE_VIDEO:
            data = np.copy(v.data)
            w, h = v.xres, v.yres
            ndi.recv_free_video_v2(self.recv, v)
            return ndi_to_rgb(data, w, h)
        return None

    def close(self):
        try:
            self.ndi.recv_destroy(self.recv)
            self.ndi.destroy()
        except Exception:
            pass


class FrameSync(threading.Thread):
    """Pulls frames from a source and pushes sampled colors to the devices."""

    def __init__(self, frame_source, devices, mode, fps, bridge,
                 ddp_port=4048, debug=False):
        super().__init__(daemon=True)
        self.source = frame_source
        self.devices = devices
        self.mode = mode
        self.interval = 1.0 / max(1, fps)
        self.bridge = bridge
        self.debug = debug
        self._stop = threading.Event()
        self._next_log = 0.0
        self._ddp = {}
        if mode == "gradient":
            self._ddp = {d.ip: DdpSender(d.ip.split(":")[0], ddp_port)
                         for d in devices}

    def stop(self):
        self._stop.set()

    def run(self):
        next_t = 0.0
        while not self._stop.is_set():
            frame = self.source.get_frame(timeout=0.5)
            if frame is None:
                continue
            now = time.time()
            if now < next_t:
                continue
            next_t = now + self.interval
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
                    self._ddp[d.ip].send(colors.tobytes())
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


# ---- Devices -------------------------------------------------------------------

class Device:
    """A WLED device plus its Modulaser mapping."""

    def __init__(self, ip, mapping, client):
        self.ip = ip
        self.name = mapping.get("name", "WLED")
        self.client = client
        self.led_count = int(mapping.get("led_count", 30))
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
        return round(max(0.0, min(1.0, (self.bpm - 20.0) / 280.0)) * 255)

    # -- OSC handlers --

    def on_global_color(self, address, *args):
        if not args:
            return
        key = self._canon_key(address.rsplit("/", 1)[-1])
        value = self._norm(args[0])
        with self.lock:
            if key == "level":
                bri = max(1, round(value * 255))
                for d in self.devices:
                    d.client.queue(top={"bri": bri})
                return
            if self.frame_sync or key not in self.global_hsl:
                return
            self.global_hsl[key] = value
            rgb = self._hsl_rgb(self.global_hsl)
        for client, seg in self._global_targets():
            client.queue(segment=seg, seg_fields={"col": [rgb]})

    def on_blackout(self, address, *args):
        if not args:
            return
        self.blackout = float(args[0]) >= 0.5
        for d in self.devices:
            d.client.queue(top={"on": not self.blackout})

    def on_group_opacity(self, address, *args):
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
        if not args or not self.cfg["bpm_sync"]:
            return
        value = float(args[0])
        bpm = 20.0 + value * 280.0 if value <= 1.0 else value
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
        if self.debug:
            d.set_default_handler(
                lambda addr, *a: print(f"OSC (unmapped): {addr} {a}"))
        return d


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

    cfg = load_config()

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

    framesync = None
    if source == "ndi":
        if np is None:
            print("numpy is not installed (pip install numpy); "
                  "falling back to OSC colors.")
        else:
            try:
                ndi_src = NdiSource(preferred=cfg.get("ndi_source"))
                cfg["ndi_source"] = ndi_src.name
                save_config(cfg)
                bridge.frame_sync = True
                framesync = FrameSync(ndi_src, devices, mode,
                                      cfg.get("ndi_fps", 30), bridge,
                                      debug=args.debug)
                framesync.start()
            except RuntimeError as e:
                print(f"{e}\nFalling back to OSC colors.")

    server = BlockingOSCUDPServer(
        (cfg["listen_ip"], cfg["listen_port"]), bridge.build_dispatcher())

    try:
        SimpleUDPClient(cfg["modulaser_ip"],
                        cfg["modulaser_osc_in_port"]).send_message("/refresh", [])
    except OSError as e:
        print(f"Could not send /refresh to Modulaser: {e}")

    print(f"\nBridging Modulaser ({cfg['listen_ip']}:{cfg['listen_port']}) "
          f"-> {len(devices)} WLED device(s)"
          + (f", color source: NDI/{mode}" if framesync else
             ", color source: OSC"))
    for d in devices:
        mapped = ", ".join(f"group {g} -> seg {s}"
                           for g, s in sorted(d.group_seg.items()))
        print(f"  {d.name} ({d.ip}): global color -> segments "
              f"{d.global_segs}; {mapped or 'no group mappings'}")
    print("Ctrl+C to quit"
          + (" (WLED state will be restored)" if restores else ""))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if framesync:
            framesync.stop()
            framesync.source.close()
        for d in devices:
            d.client.stop()
        for client, snap in restores:
            print(f"Restoring {client.ip}...")
            client.post_state(snap, timeout=3)


if __name__ == "__main__":
    main()
