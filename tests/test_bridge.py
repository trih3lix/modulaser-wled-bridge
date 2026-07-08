"""Unit tests for the pure logic in modulaser_wled_bridge.

These run without any hardware: no WLED device, no Modulaser, no NDI runtime.
numpy is required; ndi-python is not (NDI import is lazy inside NdiSource).
"""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modulaser_wled_bridge as mb


# ---- color math --------------------------------------------------------------

def test_hsl_rgb_primary_colors():
    red = mb.Bridge._hsl_rgb({"hue": 0.0, "saturation": 1.0, "lightness": 0.5})
    green = mb.Bridge._hsl_rgb({"hue": 1 / 3, "saturation": 1.0, "lightness": 0.5})
    blue = mb.Bridge._hsl_rgb({"hue": 2 / 3, "saturation": 1.0, "lightness": 0.5})
    assert red == [255, 0, 0]
    assert green == [0, 255, 0]
    assert blue == [0, 0, 255]


def test_hsl_rgb_white_and_black():
    white = mb.Bridge._hsl_rgb({"hue": 0.0, "saturation": 0.0, "lightness": 1.0})
    black = mb.Bridge._hsl_rgb({"hue": 0.0, "saturation": 0.0, "lightness": 0.0})
    assert white == [255, 255, 255]
    assert black == [0, 0, 0]


# ---- BPM <-> sx --------------------------------------------------------------

def test_bpm_to_sx_bounds():
    assert mb.bpm_to_sx(20.0) == 0
    assert mb.bpm_to_sx(300.0) == 255
    assert 120 < mb.bpm_to_sx(160.0) < 135


def test_osc_to_bpm_normalized_and_absolute():
    # normalized OSC (0..1) maps into 20..300
    assert mb.osc_to_bpm(0.0) == pytest.approx(20.0)
    assert mb.osc_to_bpm(1.0) == pytest.approx(300.0)
    # values > 1 are treated as an absolute BPM
    assert mb.osc_to_bpm(128.0) == pytest.approx(128.0)


def test_bpm_roundtrip_matches_legacy_scaling():
    # the two former inline formulas must agree through the shared helpers
    for value in (0.0, 0.25, 0.5, 0.75, 1.0):
        bpm = mb.osc_to_bpm(value)
        assert mb.bpm_to_sx(bpm) == round(
            max(0.0, min(1.0, (bpm - 20.0) / 280.0)) * 255)


# ---- NDI buffer normalization ------------------------------------------------

def test_ndi_to_rgb_clean_bgra():
    h, w = 2, 3
    # BGRA pixel: B=10, G=20, R=30, A=255  ->  RGB (30, 20, 10)
    bgra = np.zeros((h, w, 4), dtype=np.uint8)
    bgra[..., 0] = 10
    bgra[..., 1] = 20
    bgra[..., 2] = 30
    bgra[..., 3] = 255
    rgb = mb.ndi_to_rgb(bgra, w, h)
    assert rgb.shape == (h, w, 3)
    assert list(rgb[0, 0]) == [30, 20, 10]


def test_ndi_to_rgb_flat_buffer():
    h, w = 2, 3
    bgra = np.zeros((h, w, 4), dtype=np.uint8)
    bgra[..., 2] = 40  # red
    flat = bgra.reshape(-1)  # arrives 1-D
    rgb = mb.ndi_to_rgb(flat, w, h)
    assert rgb.shape == (h, w, 3)
    assert list(rgb[1, 1]) == [40, 0, 0]


def test_ndi_to_rgb_stride_padded():
    h, w = 2, 3
    pad = 8  # extra bytes per row beyond w*4
    row_bytes = w * 4 + pad
    buf = np.zeros((h, row_bytes), dtype=np.uint8)
    # write red into the first w*4 bytes of each row
    px = np.zeros((w, 4), dtype=np.uint8)
    px[:, 2] = 50
    buf[:, :w * 4] = px.reshape(-1)
    rgb = mb.ndi_to_rgb(buf, w, h)
    assert rgb.shape == (h, w, 3)
    assert list(rgb[0, 0]) == [50, 0, 0]


# ---- frame sampling ----------------------------------------------------------

def _solid(h, w, rgb):
    a = np.zeros((h, w, 3), dtype=np.uint8)
    a[:] = rgb
    return a


def test_sample_dominant_ignores_black_background():
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    frame[2:5, 2:5] = [200, 0, 0]  # small red patch on black
    dom = mb.sample_dominant(frame)
    assert dom[0] > 150 and dom[1] < 40 and dom[2] < 40


def test_sample_dominant_all_black():
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    assert list(mb.sample_dominant(frame)) == [0, 0, 0]


def test_sample_gradient_left_red_right_blue():
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    frame[:, :10] = [255, 0, 0]
    frame[:, 10:] = [0, 0, 255]
    out = mb.sample_gradient(frame, 2)
    assert out.shape == (2, 3)
    assert out[0][0] > 200 and out[0][2] < 40   # left bin red
    assert out[1][2] > 200 and out[1][0] < 40   # right bin blue


def test_sample_gradient_count_matches():
    frame = _solid(6, 30, [100, 100, 100])
    for count in (1, 5, 30, 59):
        assert mb.sample_gradient(frame, count).shape == (count, 3)


def test_sample_zones_splits_frame():
    frame = np.zeros((10, 30, 3), dtype=np.uint8)
    frame[:, :10] = [255, 0, 0]
    frame[:, 10:20] = [0, 255, 0]
    frame[:, 20:] = [0, 0, 255]
    out = mb.sample_zones(frame, 3)
    assert out.shape == (3, 3)
    assert out[0][0] > 200
    assert out[1][1] > 200
    assert out[2][2] > 200


# ---- DDP packet framing ------------------------------------------------------

class _FakeSock:
    def __init__(self):
        self.packets = []

    def sendto(self, data, addr):
        self.packets.append((bytes(data), addr))


def _make_sender():
    s = mb.DdpSender("10.0.0.5")
    s.sock = _FakeSock()
    return s


def test_ddp_single_packet_header():
    s = _make_sender()
    data = bytes([1, 2, 3]) * 4  # 12 bytes, one packet
    assert s.send(data) is True
    assert len(s.sock.packets) == 1
    pkt, addr = s.sock.packets[0]
    assert addr == ("10.0.0.5", 4048)
    flags = pkt[0]
    assert flags == 0x41            # version 1 (0x40) | push (0x01), last packet
    assert pkt[1] == 1              # sequence starts at 1
    assert pkt[2] == 0x01 and pkt[3] == 0x01  # data type / source id
    assert int.from_bytes(pkt[4:8], "big") == 0            # offset
    assert int.from_bytes(pkt[8:10], "big") == len(data)   # length
    assert pkt[10:] == data


def test_ddp_sequence_wraps_1_to_15():
    s = _make_sender()
    seqs = []
    for _ in range(17):
        s.send(b"\x00\x00\x00")
        seqs.append(s.sock.packets[-1][0][1])
    assert seqs[:15] == list(range(1, 16))
    assert seqs[15] == 1  # wrap back to 1 (never 0)
    assert seqs[16] == 2


def test_ddp_multi_packet_split_and_offsets():
    s = _make_sender()
    n = mb.DdpSender.MAX_DATA + 30  # spills into a second packet
    data = bytes(n)
    s.send(data)
    assert len(s.sock.packets) == 2
    p0, p1 = s.sock.packets[0][0], s.sock.packets[1][0]
    assert p0[0] == 0x40  # not last -> no push flag
    assert int.from_bytes(p0[4:8], "big") == 0
    assert int.from_bytes(p0[8:10], "big") == mb.DdpSender.MAX_DATA
    assert p1[0] == 0x41  # last -> push
    assert int.from_bytes(p1[4:8], "big") == mb.DdpSender.MAX_DATA
    assert int.from_bytes(p1[8:10], "big") == 30


def test_ddp_length_fit_clips_and_pads():
    s = _make_sender()
    # too short: 2 LEDs of data for a 4-LED strip -> padded to 12 bytes
    s.send(bytes([9, 9, 9, 8, 8, 8]), led_count=4)
    payload = s.sock.packets[-1][0][10:]
    assert len(payload) == 12
    assert payload[:6] == bytes([9, 9, 9, 8, 8, 8])
    assert payload[6:] == bytes(6)
    # too long: clipped to 3 bytes
    s2 = _make_sender()
    s2.send(bytes(range(9)), led_count=1)
    assert len(s2.sock.packets[-1][0][10:]) == 3


def test_ddp_send_error_kept_silent_and_returns_false():
    s = _make_sender()

    def boom(data, addr):
        raise OSError("network down")

    s.sock.sendto = boom
    assert s.send(b"\x00\x00\x00") is False  # does not raise


# ---- config load / migration -------------------------------------------------

def test_load_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CONFIG_PATH", tmp_path / "none.yaml")
    cfg = mb.load_config()
    assert cfg["listen_port"] == 9000
    assert cfg["effects"] == {"solid": 0, "strobe": 23, "chase": 28}
    assert cfg["devices"] == {}


def test_load_config_migrates_v2_single_device(tmp_path, monkeypatch):
    p = tmp_path / "wled_bridge.yaml"
    p.write_text(
        "wled_ip: 192.168.0.42\n"
        "global_color_segments: [0, 1]\n"
        "groups:\n  0: 0\n")
    monkeypatch.setattr(mb, "CONFIG_PATH", p)
    cfg = mb.load_config()
    assert "wled_ip" not in cfg
    assert "192.168.0.42" in cfg["devices"]
    dev = cfg["devices"]["192.168.0.42"]
    assert dev["global_color_segments"] == [0, 1]
    assert cfg["selected"] == ["192.168.0.42"]


def _write_cfg(tmp_path, monkeypatch, text):
    p = tmp_path / "wled_bridge.yaml"
    p.write_text(text)
    monkeypatch.setattr(mb, "CONFIG_PATH", p)


def test_validate_bad_led_count_names_key(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch,
               "devices:\n  192.168.0.5:\n    led_count: not-a-number\n")
    with pytest.raises(mb.ConfigError) as ei:
        mb.load_config()
    assert "devices.192.168.0.5.led_count" in str(ei.value)


def test_validate_non_dict_devices(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, "devices: [1, 2, 3]\n")
    with pytest.raises(mb.ConfigError) as ei:
        mb.load_config()
    assert "devices" in str(ei.value)


def test_validate_bad_effect_type(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, "effects:\n  strobe: fast\n")
    with pytest.raises(mb.ConfigError) as ei:
        mb.load_config()
    assert "effects.strobe" in str(ei.value)


def test_validate_bad_color_source(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, "color_source: laser\n")
    with pytest.raises(mb.ConfigError):
        mb.load_config()


def test_validate_groups_must_be_mapping(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch,
               "devices:\n  10.0.0.1:\n    groups: [0, 1]\n")
    with pytest.raises(mb.ConfigError) as ei:
        mb.load_config()
    assert "groups" in str(ei.value)


def test_local_subnet_honors_explicit_cidr():
    net = mb.local_subnet("192.168.5.0/25")
    assert str(net) == "192.168.5.0/25"
    assert net.num_addresses == 128


def test_validate_bad_sweep_cidr(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, "sweep_cidr: not-a-cidr\n")
    with pytest.raises(mb.ConfigError) as ei:
        mb.load_config()
    assert "sweep_cidr" in str(ei.value)


def test_load_config_merges_partial_effects(tmp_path, monkeypatch):
    p = tmp_path / "wled_bridge.yaml"
    p.write_text("effects:\n  strobe: 99\n")
    monkeypatch.setattr(mb, "CONFIG_PATH", p)
    cfg = mb.load_config()
    assert cfg["effects"]["strobe"] == 99
    assert cfg["effects"]["solid"] == 0   # default preserved
    assert cfg["effects"]["chase"] == 28


# ---- WledClient debounce / requeue / shutdown --------------------------------

class _RecordingSession:
    """Stands in for requests.Session; records posts, optionally fails."""

    def __init__(self, fail=False):
        self.fail = fail
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        if self.fail:
            import requests
            raise requests.RequestException("boom")

    def close(self):
        pass


def _client(fail=False):
    c = mb.WledClient("10.0.0.7", max_rate_hz=20)
    c._session = _RecordingSession(fail=fail)
    return c


def test_take_pending_merges_segments():
    c = _client()
    c.queue(top={"bri": 100})
    c.queue(segment=0, seg_fields={"col": [[1, 2, 3]]})
    c.queue(segment=0, seg_fields={"fx": 5})
    c.queue(segment=1, seg_fields={"col": [[9, 9, 9]]})
    raws, payload = c._take_pending()
    assert raws == []
    assert payload["bri"] == 100
    segs = {s["id"]: s for s in payload["seg"]}
    assert segs[0]["col"] == [[1, 2, 3]] and segs[0]["fx"] == 5
    assert segs[1]["col"] == [[9, 9, 9]]
    # queue emptied
    assert c._take_pending() == ([], None)


def test_queue_raw_is_returned_separately():
    c = _client()
    c.queue_raw({"ps": 3})
    c.queue(top={"on": True})
    raws, payload = c._take_pending()
    assert raws == [{"ps": 3}]
    assert payload == {"on": True}


def test_begin_shutdown_freezes_queues():
    c = _client()
    c.begin_shutdown()
    c.queue(top={"bri": 50})
    c.queue_raw({"ps": 1})
    assert c._take_pending() == ([], None)


def test_post_state_requeues_on_failure():
    c = _client(fail=True)
    c.post_state({"bri": 42, "seg": [{"id": 0, "col": [[1, 1, 1]]}]})
    # failed payload merged back into pending for retry
    _, payload = c._take_pending()
    assert payload["bri"] == 42
    assert payload["seg"][0]["id"] == 0
    assert c._online is False


def test_post_state_no_requeue_during_shutdown():
    c = _client(fail=True)
    c.begin_shutdown()
    c.post_state({"bri": 42})
    assert c._take_pending() == ([], None)  # nothing requeued
