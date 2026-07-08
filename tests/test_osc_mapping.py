"""OSC-dispatch mapping tests: exercise Bridge handlers with fake WLED clients
so no network or hardware is touched."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modulaser_wled_bridge as mb


class FakeClient:
    """Records queue()/queue_raw() calls instead of hitting a device."""

    def __init__(self, ip="10.0.0.9"):
        self.ip = ip
        self.tops = []
        self.segs = []   # (segment, seg_fields)
        self.raws = []

    def queue(self, top=None, segment=None, seg_fields=None):
        if top:
            self.tops.append(top)
        if segment is not None and seg_fields:
            self.segs.append((segment, seg_fields))

    def queue_raw(self, payload):
        self.raws.append(payload)


def make_bridge():
    cfg = dict(mb.DEFAULT_CONFIG)
    cfg["effects"] = dict(mb.DEFAULT_CONFIG["effects"])
    client = FakeClient()
    mapping = {"name": "T", "led_count": 10,
               "global_color_segments": [0, 1], "groups": {0: 0, 1: [1, 2]}}
    dev = mb.Device(client.ip, mapping, client)
    bridge = mb.Bridge(cfg, [dev])
    return bridge, client


def test_global_hue_sets_segment_color():
    bridge, client = make_bridge()
    bridge.on_global_color("/output/color/hue", 0.0)  # red
    # both global segments get a red color update
    assert len(client.segs) == 2
    for seg, fields in client.segs:
        assert fields["col"] == [[255, 0, 0]]
    assert {s for s, _ in client.segs} == {0, 1}


def test_global_level_sets_brightness_top():
    bridge, client = make_bridge()
    bridge.on_global_color("/output/color/level", 1.0)
    assert client.tops[-1] == {"bri": 255}
    assert bridge.base_bri == 255


def test_blackout_toggles_on_flag():
    bridge, client = make_bridge()
    bridge.on_blackout("/output/blackout", 1.0)
    assert bridge.blackout is True
    assert client.tops[-1] == {"on": False}
    bridge.on_blackout("/output/blackout", 0.0)
    assert bridge.blackout is False
    assert client.tops[-1] == {"on": True}


def test_group_opacity_sets_segment_brightness():
    bridge, client = make_bridge()
    bridge.on_group_opacity("/group/1/opacity", 0.5)
    # group 1 maps to segments [1, 2]
    segs = {s for s, _ in client.segs}
    assert segs == {1, 2}
    for _, fields in client.segs:
        assert fields["bri"] == round(0.5 * 255)


def test_group_strobe_enable_switches_effect():
    bridge, client = make_bridge()
    bridge.on_group_fx("/group/0/fx/strobe/enabled", 1.0)
    # group 0 maps to segment 0, effect id should be the strobe id
    assert client.segs[-1][0] == 0
    assert client.segs[-1][1]["fx"] == bridge.fx["strobe"]


def test_group_colorize_sets_color():
    bridge, client = make_bridge()
    bridge.on_group_fx("/group/0/fx/colorize/hue", 2 / 3)  # blue
    seg, fields = client.segs[-1]
    assert seg == 0
    assert fields["col"] == [[0, 0, 255]]


def test_bpm_updates_running_effects_speed():
    bridge, client = make_bridge()
    # put an effect on (client, seg) that is not solid/strobe
    bridge.active_fx[(client, 0)] = bridge.fx["chase"]
    bridge.on_bpm("/bpm/value", 0.5)
    assert bridge.bpm == pytest.approx(mb.osc_to_bpm(0.5))
    # a speed update should have been queued for the chase segment
    assert any(f.get("sx") == mb.bpm_to_sx(bridge.bpm)
               for _, f in client.segs)


def test_bpm_sync_disabled_is_noop():
    bridge, client = make_bridge()
    bridge.cfg["bpm_sync"] = False
    bridge.on_bpm("/bpm/value", 0.9)
    assert client.segs == []
