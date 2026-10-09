"""Milestone 2: stream-health detectors (connection, source, presenter/face, frozen, audio).

Synthetic frames and a scripted face backend. The real YuNet model is only
smoke-tested (loads, no false positive on a blank frame); accuracy on real
camera footage is NOT verified here.
"""
from __future__ import annotations

import random

import pytest
from PIL import Image, ImageDraw

from studio_monitor.bots import CAT_STREAM
from studio_monitor.broadcast import LiveRules
from studio_monitor.detectors.audio import read_meter
from studio_monitor.detectors.presenter import Face, PresenterAnalyzer, YuNetDetector
from studio_monitor.detectors.suite import DetectorSuite, DetectorsConfig
from studio_monitor.detectors.text_rules import ConnectionRules, SustainedCondition
from studio_monitor.regions import Region
from conftest import FakeClock, all_deliveries
from test_activity import LIVE_TEXT, NOT_LIVE_TEXT, Harness

from pathlib import Path

CONN_RULES = Path(__file__).resolve().parents[1] / "rules" / "connection_rules.json"
W, H = 640, 360
FACE_BOX = (0.0, 0.0, 0.5, 1.0)       # left half of the frame is the presenter region


# ---------------------------------------------------------------- synthetic frames

class Scene:
    """Builds frames: background with optional moving element, optional 'face' marker (magenta square),
    optional small face jitter, optional black preview, plus a ticking clock area outside the region."""

    def __init__(self):
        self.t = 0
        self.rng = random.Random(1)

    def frame(self, face=True, face_shift=0, background_motion=False, black=False, freeze_region=False,
              tick_outside=True, noise=True) -> Image.Image:
        self.t += 1
        img = Image.new("RGB", (W, H), (30, 30, 34))
        d = ImageDraw.Draw(img)
        region_left = W // 2
        if black:
            d.rectangle((0, 0, region_left, H), fill=(0, 0, 0))
        else:
            d.rectangle((0, 0, region_left, H), fill=(70, 80, 90))
            if background_motion and not freeze_region:
                x = (self.t * 23) % (region_left - 40)
                d.rectangle((x, H - 60, x + 30, H - 20), fill=(220, 220, 40))
            if face:
                fx, fy = 110 + face_shift, 90 + (face_shift // 2)
                d.rectangle((fx, fy, fx + 100, fy + 120), fill=(255, 0, 255))     # face marker
                d.ellipse((fx + 20, fy + 30, fx + 40, fy + 50), fill=(0, 0, 0))      # 'eye' detail for texture
        if noise:   # camera sensor noise (+-3) over the presenter region, like any live camera feed
            import numpy as np
            arr = np.asarray(img).astype(np.int16)
            rs = np.random.RandomState(self.t)
            arr[:, :region_left, :] += rs.randint(-3, 4, size=arr[:, :region_left, :].shape)
            img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
            d = ImageDraw.Draw(img)
        if tick_outside:
            d.text((W - 120, 20), f"viewers {self.t:05d}", fill=(255, 255, 255))
            d.rectangle((W - 150, H - 40, W - 150 + (self.t % 100), H - 20), fill=(200, 60, 60))
        return img


class MarkerFaceBackend:
    """Scripted backend: 'detects' the magenta marker as a face (box around it)."""
    name = "marker"

    def detect(self, image: Image.Image) -> list[Face]:
        small = image.convert("RGB").resize((image.width // 4, image.height // 4))
        px = small.load()
        xs, ys = [], []
        for y in range(small.height):
            for x in range(small.width):
                r, g, b = px[x, y]
                if r > 200 and b > 200 and g < 60:
                    xs.append(x); ys.append(y)
        if not xs:
            return []
        x0, x1, y0, y1 = min(xs) * 4, (max(xs) + 1) * 4, min(ys) * 4, (max(ys) + 1) * 4
        return [Face((x0, y0, x1 - x0, y1 - y0), [(0.0, 0.0)] * 5, 0.9)]


def suite(clock, **over) -> DetectorSuite:
    cfg = DetectorsConfig(face_absent_seconds=30, motion_low_seconds=60, frozen_seconds=20, black_preview_seconds=20,
                          connection_sustain_seconds=10, source_sustain_seconds=10, recover_seconds=15,
                          connection_recover_seconds=20, audio_enabled=True, audio_silence_seconds=30, **over)
    return DetectorSuite(cfg, ConnectionRules.load(CONN_RULES), MarkerFaceBackend(), clock, clock)


def regions():
    return [Region("presenter", *FACE_BOX, kind="face"), Region("meter", 0.55, 0.55, 0.4, 0.06, kind="audio")]


def drive(s: DetectorSuite, clock: FakeClock, scene: Scene, seconds: float, step=2.0, live=True, fresh=True,
          text=LIVE_TEXT, regs=None, **frame_kwargs):
    confirmed, recovered = [], []
    for _ in range(int(seconds / step)):
        frame = scene.frame(**frame_kwargs) if fresh else None
        out = s.evaluate(frame, fresh, live, text, regs if regs is not None else regions())
        confirmed += out.confirmed
        recovered += out.recovered
        clock.advance(step)
    return [c[0] for c in confirmed], [r[0] for r in recovered]


# ---------------------------------------------------------------- units

def test_sustained_condition_pauses_on_invalid_and_recovers():
    c = SustainedCondition("X", 10, 5)
    assert c.update(True, True, 0) is None
    assert c.update(True, False, 100) is None           # invalid observations do not confirm
    assert c.confirmed is False and c.unknown is True
    assert c.update(True, True, 9) is None
    assert c.update(True, True, 10) == "confirmed" and c.episodes == 1
    assert c.update(False, True, 12) is None             # not yet recovered
    assert c.update(False, True, 17) == "recovered"
    assert c.update(True, True, 20) is None and c.update(True, True, 30) == "confirmed" and c.episodes == 2


def test_connection_rules_classify_text():
    r = ConnectionRules.load(CONN_RULES)
    assert r.verified is False
    assert r.classify("LIVE 00:10  Reconnecting... please wait")["reconnecting"] == "reconnecting"
    assert r.classify("Your LIVE has ended. Thanks for watching")["ended"]
    assert r.classify("Scene 1  Camera unavailable  Add source")["source_missing"] == "camera unavailable"
    assert all(v is None for v in r.classify(LIVE_TEXT).values())
    assert r.classify("The reconnection of my router was fine")["reconnecting"] is None     # whole-word only


def test_audio_meter_silence_vs_unreadable():
    img = Image.new("RGB", (200, 40), (20, 20, 20))
    d = ImageDraw.Draw(img)
    # readable meter track (dark segments) but nothing lit -> silence
    for i in range(20):
        d.rectangle((i * 10, 10, i * 10 + 8, 30), fill=(35, 35, 35))
    silent = read_meter(img, (0, 0, 200, 40))
    assert silent.valid and silent.level == 0.0
    # lit meter -> activity
    for i in range(12):
        d.rectangle((i * 10, 10, i * 10 + 8, 30), fill=(40, 220, 60))
    loud = read_meter(img, (0, 0, 200, 40))
    assert loud.valid and loud.level > 0.2
    # flat region (meter hidden / wrong calibration) -> unreadable, never "silence"
    flat = read_meter(Image.new("RGB", (200, 40), (20, 20, 20)), (0, 0, 200, 40))
    assert flat.valid is False


def test_presenter_analyzer_in_face_motion_ignores_background():
    an = PresenterAnalyzer(MarkerFaceBackend())
    sc = Scene()
    box = (0, 0, W // 2, H)
    an.observe(sc.frame(background_motion=True), box, True)
    still = an.observe(sc.frame(background_motion=True), box, True)
    assert still.face is not None and still.motion < 0.035 and still.region_changed
    moved = an.observe(sc.frame(background_motion=True, face_shift=12), box, True)
    assert moved.motion > 0.035
    assert an.observe(sc.frame(), box, fresh=False).valid is False


# ---------------------------------------------------------------- suite scenarios

def test_face_absent_vs_still_vs_capture_unavailable():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    # presenter still (same marker) for 40 s: no FACE_ABSENT, FACE_MOTION_LOW only at 60 s
    conf, _ = drive(s, clock, sc, 40, face=True)
    assert conf == []
    conf, _ = drive(s, clock, sc, 24, face=True)
    assert conf == ["FACE_MOTION_LOW"]
    # presenter leaves: FACE_ABSENT after 30 s, still condition recovers (no face)
    conf, rec = drive(s, clock, sc, 32, face=False)
    assert "FACE_ABSENT" in conf and "FACE_MOTION_LOW" in rec
    # capture unavailable for a long time: UNKNOWN, nothing new confirmed or recovered
    conf, rec = drive(s, clock, sc, 120, fresh=False)
    assert conf == [] and rec == []
    assert s.snapshot()["FACE_ABSENT"].state == "UNKNOWN"
    # presenter returns -> recovery after 15 s
    conf, rec = drive(s, clock, sc, 18, face=True, face_shift=5)
    assert rec == ["FACE_ABSENT"]


def test_background_movement_does_not_count_as_face_movement():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    conf, _ = drive(s, clock, sc, 64, face=True, background_motion=True)
    assert conf == ["FACE_MOTION_LOW"]
    assert s.snapshot()["PREVIEW_FROZEN"].state == "OK"       # the region kept changing (background), so not frozen


def test_face_moving_never_raises_still_alert():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    confirmed = []
    for i in range(40):
        out = s.evaluate(sc.frame(face_shift=(i % 4) * 10), True, True, LIVE_TEXT, regions())
        confirmed += out.confirmed
        clock.advance(2)
    assert confirmed == []


def test_frozen_preview_requires_changing_rest_of_frame():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    frozen = sc.frame(face=True)
    # identical region AND identical whole frame = static WGC stream: UNKNOWN-ish, must NOT be frozen
    confirmed = []
    for _ in range(15):
        confirmed += [c[0] for c in s.evaluate(frozen, True, True, LIVE_TEXT, regions()).confirmed]
        clock.advance(2)
    assert "PREVIEW_FROZEN" not in confirmed
    # region frozen while the viewer counter outside keeps ticking -> PREVIEW_FROZEN after 20 s
    confirmed = []
    for _ in range(15):
        live = sc.frame(face=True)
        live.paste(frozen.crop((0, 0, W // 2, H)), (0, 0))
        confirmed += [c[0] for c in s.evaluate(live, True, True, LIVE_TEXT, regions()).confirmed]
        clock.advance(2)
    assert "PREVIEW_FROZEN" in confirmed


def test_source_error_vs_ordinary_black_preview():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    conf, _ = drive(s, clock, sc, 24, black=True)
    assert conf == ["BLACK_PREVIEW"]                              # black, no face alert (black is not "absent")
    s2 = suite(clock); sc2 = Scene()
    conf, _ = drive(s2, clock, sc2, 14, text=LIVE_TEXT + "  Camera unavailable")
    assert conf == ["SOURCE_MISSING"]


def test_reconnection_recovery_and_duplicate_suppression():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    conf, _ = drive(s, clock, sc, 8, text="LIVE 00:01  Reconnecting...")
    assert conf == []                                            # below sustain threshold
    conf, _ = drive(s, clock, sc, 40, text="LIVE 00:01  Reconnecting...")
    assert conf == ["RECONNECTING"]                             # exactly one confirmation while it persists
    conf, rec = drive(s, clock, sc, 10, text=LIVE_TEXT)
    assert rec == []                                            # brief clear does not count yet
    conf, rec = drive(s, clock, sc, 14, text=LIVE_TEXT)
    assert rec == ["RECONNECTING"] and s.reconnect_episodes == 1
    conf, _ = drive(s, clock, sc, 14, text="connection lost")
    assert conf == ["RECONNECTING"] and s.reconnect_episodes == 2


def test_not_live_or_suppressed_means_unknown():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    conf, _ = drive(s, clock, sc, 60, live=False, face=False, text="Reconnecting")
    assert conf == []
    conf, _ = drive(s, clock, sc, 60, face=False)                # now live: counts from here
    assert conf == ["FACE_ABSENT"]
    s.suspend(30)
    out = s.evaluate(sc.frame(face=False), True, True, LIVE_TEXT, regions())
    assert out.conditions["FACE_ABSENT"].state == "UNKNOWN"


def test_scene_change_grace_resets_presenter_timers():
    clock = FakeClock(); sc = Scene()
    s = suite(clock, scene_change_grace_seconds=10)
    drive(s, clock, sc, 20, face=False)
    white = Image.new("RGB", (W, H), (250, 250, 250))
    out = s.evaluate(white, True, True, LIVE_TEXT, regions())
    assert s.scene_changes == 1 and s.in_grace
    conf, _ = drive(s, clock, sc, 20, face=False)                # 10 s grace + 10 s < 30 s threshold
    assert conf == []


def test_audio_silence_detected_only_with_readable_meter():
    clock = FakeClock(); sc = Scene()
    s = suite(clock)
    # the Scene frame has a flat area at the meter region -> unreadable -> UNKNOWN, never silence
    conf, _ = drive(s, clock, sc, 60)
    assert "AUDIO_SILENCE" not in conf
    assert s.snapshot()["AUDIO_SILENCE"].state == "UNKNOWN"
    # draw a readable dark meter track into the region -> silence after 30 s
    confirmed = []
    for _ in range(18):
        f = sc.frame()
        d = ImageDraw.Draw(f)
        box = regions()[1].to_box(W, H)
        for i in range(20):
            d.rectangle((box[0] + i * 10, box[1] + 2, box[0] + i * 10 + 7, box[3] - 2), fill=(45, 45, 45))
        confirmed += [c[0] for c in s.evaluate(f, True, True, LIVE_TEXT, regions()).confirmed]
        clock.advance(2)
    assert "AUDIO_SILENCE" in confirmed


def test_disabled_presenter_reports_disabled():
    clock = FakeClock()
    cfg = DetectorsConfig(presenter_enabled=False)
    s = DetectorSuite(cfg, ConnectionRules({}), None, clock, clock)
    snap = s.evaluate(Scene().frame(), True, True, LIVE_TEXT, regions()).conditions
    assert snap["FACE_ABSENT"].state == "DISABLED" and snap["AUDIO_SILENCE"].state == "DISABLED"


# ---------------------------------------------------------------- real model smoke test

@pytest.mark.skipif(not YuNetDetector.available(), reason="YuNet model not bundled")
def test_yunet_loads_and_has_no_false_positive_on_synthetic_frame():
    det = YuNetDetector()
    assert det.detect(Scene().frame()) == []                     # magenta square is not a face
    assert det.detect(Image.new("RGB", (320, 320), (90, 90, 90))) == []


# ---------------------------------------------------------------- Monitor integration

def _stream_alerts(h):
    return [a for a in all_deliveries(h.queue) if a["kind"] == "incident" and a["event_id"].startswith("STR")]


def _live_harness(cfg, rules, clock):
    cfg.regions = []          # text conditions only; the fake frame has no presenter
    h = Harness(cfg, rules, clock)
    h.mon.detectors = suite(clock)
    h.ocr.default = LIVE_TEXT
    h.run(30)                                                    # confirm LIVE
    assert h.mon.broadcast.state.state.value == "LIVE"
    return h


def test_monitor_reconnecting_incident_with_recovery_and_screenshot(cfg, rules, clock):
    h = _live_harness(cfg, rules, clock)
    h.ocr.default = LIVE_TEXT + " Reconnecting..."
    h.run(16)
    alerts = _stream_alerts(h)
    primary = [a for a in alerts if a["event_id"].count("-") == 3]
    assert len(primary) == 1
    assert "CONNECTION PROBLEM" in primary[0]["payload"]["text"] and "not yet verified" in primary[0]["payload"]["text"]
    assert primary[0]["screenshot_path"].endswith(".png")
    assert h.mon.broadcast.state.state.value in ("LIVE", "UNKNOWN")   # a reconnecting overlay never means NOT_LIVE
    assert h.mon.episodes.state.episode_id                          # the LIVE episode stays open
    inc = h.mon.incident_engine.get(primary[0]["event_id"])
    assert inc is not None and inc.is_open and inc.category == CAT_STREAM
    h.ocr.default = LIVE_TEXT
    h.run(30)
    res = [a for a in _stream_alerts(h) if a["event_id"].endswith("-RES")]
    assert len(res) == 1 and "CLEARED" in res[0]["payload"]["text"] and res[0]["payload"]["thread_of"] == primary[0]["event_id"]
    assert not h.mon.incident_engine.get(primary[0]["event_id"]).is_open
    h.run(60)
    assert len([a for a in _stream_alerts(h) if a["event_id"].count("-") == 3]) == 1   # no duplicates


def test_monitor_does_not_evaluate_when_not_live(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.mon.detectors = suite(clock)
    h.ocr.default = NOT_LIVE_TEXT + " Reconnecting"
    h.run(90)
    assert _stream_alerts(h) == []
    assert h.mon.activity.stream == {}


def test_monitor_closes_stream_incidents_when_broadcast_ends(cfg, rules, clock):
    h = _live_harness(cfg, rules, clock)
    h.ocr.default = LIVE_TEXT + " Camera unavailable"
    h.run(16)
    primary = [a for a in _stream_alerts(h) if a["event_id"].count("-") == 3]
    assert len(primary) == 1 and "SOURCE MISSING" in primary[0]["payload"]["text"]
    h.ocr.default = NOT_LIVE_TEXT
    h.run(40)
    assert h.mon.broadcast.state.state.value == "NOT_LIVE"
    inc = h.mon.incident_engine.get(primary[0]["event_id"])
    assert not inc.is_open
    assert not [a for a in _stream_alerts(h) if a["event_id"].endswith("-RES")]   # closed quietly, no recovery alert


def test_monitor_maintenance_suppresses_stream_alerts(cfg, rules, clock):
    h = _live_harness(cfg, rules, clock)
    h.mon.incident_engine.enter_maintenance(h.mon.device_id, 3600, [CAT_STREAM], "break")
    h.ocr.default = LIVE_TEXT + " Reconnecting"
    h.run(60)
    assert _stream_alerts(h) == []
