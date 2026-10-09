"""End-to-end automatic perception inside the Monitor: no manual regions,
synthetic Studio frames as the capture, OCR double with boxes, the presenter
detector fed by the discovered preview, audio worker status, profile-click
gating, privacy masks, relocalization after restart."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeClock, FakeWindowSystem, all_deliveries, make_window
from studio_monitor.audio.levels import AudioAnalyzer, AudioConfig, EnergyVad
from studio_monitor.audio.resolver import AudioEndpoint, AudioSession, AudioSourceResolver, KIND_LOOPBACK, KIND_VISUAL
from studio_monitor.audio.worker import AudioWorker
from studio_monitor.detectors.suite import DetectorSuite, DetectorsConfig
from studio_monitor.detectors.text_rules import ConnectionRules
from studio_monitor.perception.layout import LayoutStore
from studio_monitor.perception.tracker import LayoutTracker
from studio_monitor.regions import Region
from studio_monitor.win32.capture import Capture
from studio_monitor.win32.windows import Rect
from studio_synth import BoxOcr, StudioScene, render
from test_activity import Harness, LIVE_TEXT, NOT_LIVE_TEXT
from test_detectors import MarkerFaceBackend

W, H = 1280, 720
CONN_RULES = Path(__file__).resolve().parents[1] / "rules" / "connection_rules.json"


class SceneCapturer:
    """Capture double that returns the rendered Studio scene for the Studio window."""

    def __init__(self, ocr: BoxOcr):
        self.ocr = ocr
        self.scene = StudioScene(width=W, height=H, live=False)
        self.calls: list[int] = []
        self._cache = None
        self.enabled = True

    def set_scene(self, **kw):
        for k, v in kw.items():
            setattr(self.scene, k, v)
        self._cache = None

    def frame(self):
        if self._cache is None:
            rf = render(self.scene)
            self.ocr.set_frame(rf.image, rf.boxes)
            self._cache = rf
        return self._cache

    def capture(self, window, foreground_hwnd=0):
        self.calls.append(window.hwnd)
        if window.minimized or not self.enabled:
            return None
        rf = self.frame()
        return Capture(rf.image.copy(), window, "printwindow", True, hwnd=window.hwnd)


class AutoHarness(Harness):
    def __init__(self, cfg, rules, clock, tmp_path, audio=None):
        self.box_ocr = BoxOcr()
        self.scene_cap = SceneCapturer(self.box_ocr)
        sys_ = FakeWindowSystem()
        sys_.add(make_window(rect=Rect(100, 100, 100 + W, 100 + H)))
        super().__init__(cfg, rules, clock, sys_=sys_)
        self.cap = self.scene_cap
        self.mon.capturer = self.scene_cap
        self.mon.frames = _SyncFrames(self.scene_cap, sys_, clock)
        self.ocr = self.box_ocr
        self.mon.detector.ocr = self.box_ocr
        self.mon.perception = LayoutTracker(self.box_ocr, LayoutStore(tmp_path / "layouts.json"), uia_probe=lambda: ([], "none"),
                                            discovery_interval=60, validate_interval=5, mono=clock, clock=clock, on_event=self.events.append)
        self.mon.detectors = DetectorSuite(DetectorsConfig(face_absent_seconds=20, recover_seconds=10), ConnectionRules.load(CONN_RULES),
                                           MarkerFaceBackend(), clock, clock)
        self.mon.audio_worker = audio

    def go_live(self):
        self.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
        self.run(30)
        assert self.mon.broadcast.state.state.value == "LIVE"


class _SyncFrames:
    """Minimal FrameService over the scene capturer (fresh seq per tick)."""

    def __init__(self, cap, system, clock):
        self.cap, self.system, self.clock, self.seq, self.hwnd = cap, system, clock, 0, 0

    def bind(self, hwnd):
        self.hwnd = hwnd

    def frame(self, max_age=None):
        w = self.system.get_window(self.hwnd) if self.hwnd else None
        if w is None:
            return None
        c = self.cap.capture(w)
        if c is None:
            return None
        self.seq += 1
        c.seq, c.captured_at, c.captured_mono = self.seq, self.clock(), self.clock()
        return c

    def status(self):
        from studio_monitor.win32.capture import CaptureStatus
        return CaptureStatus(hwnd=self.hwnd, backend="scene", health="OK")

    def stop(self):
        pass


@pytest.fixture
def auto(cfg, rules, clock, tmp_path):
    cfg.perception.enabled = True
    cfg.regions = []                                  # no manual regions at all
    return AutoHarness(cfg, rules, clock, tmp_path)


# ---------------------------------------------------------------- flows

def test_start_discovers_layout_and_feeds_detectors_without_manual_regions(auto):
    h = auto
    h.run(6)
    assert h.mon.layout_state() == "detected" and h.mon.activity.perception["elements"]["program_preview"]
    kinds = {r.kind for r in h.mon.effective_regions()}
    assert {"face", "audio", "profile", "redact"} <= kinds               # presenter, meter, profile and the auto chat mask
    assert h.cfg.regions == []                                            # nothing was written into the manual regions
    h.go_live()
    assert h.mon.presenter_state() == "detected"
    h.scene_cap.set_scene(face_boxes=[])                                  # presenter leaves the preview
    h.run(26)
    alerts = [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STRFAC")]
    assert len(alerts) == 1 and "PRESENTER NOT VISIBLE" in alerts[0]["payload"]["text"]
    assert h.mon.presenter_state() == "absent"


def test_thumbnail_and_avatar_faces_are_not_the_presenter(auto):
    h = auto
    h.scene_cap.set_scene(thumbnails=2, chat_avatars=3)
    h.run(6)
    h.go_live()
    h.scene_cap.set_scene(face_boxes=[], thumbnails=2, chat_avatars=3)   # faces remain only in thumbnails/avatars
    h.run(26)
    assert h.mon.presenter_state() == "absent"
    assert [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STRFAC")]


def test_multiple_faces_report_unclear_not_a_random_choice(auto):
    h = auto
    h.run(6)
    h.scene_cap.set_scene(live=True, face_boxes=[(40, 60, 80, 100), (300, 60, 80, 100)])
    h.run(30)
    assert h.mon.presenter_state().startswith("unclear")
    assert not [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STRFAC")]
    assert h.mon.activity.stream["FACE_ABSENT"]["state"] == "UNKNOWN"


def test_layout_failure_reports_unavailable_instead_of_absent(auto):
    h = auto
    h.box_ocr.with_geometry = False                                       # degraded OCR: no boxes -> no layout
    h.go_live()
    h.run(30)
    assert h.mon.layout_state() == "failed"
    assert h.mon.presenter_state() == "unavailable"
    assert "Presenter region unavailable" in h.mon.activity.stream["FACE_ABSENT"]["detail"]
    assert "Locating audio meter" in h.mon.activity.stream["AUDIO_SILENCE"]["detail"]
    assert not [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STR")]


def test_manual_regions_and_masks_take_precedence(auto):
    h = auto
    h.cfg.regions = [Region("my presenter", 0.1, 0.1, 0.2, 0.2, kind="face"), Region("mask", 0.0, 0.0, 0.1, 0.1, kind="redact")]
    h.run(6)
    regs = h.mon.effective_regions()
    faces = [r for r in regs if r.kind == "face"]
    assert len(faces) == 1 and faces[0].name == "my presenter"            # automatic presenter region not added
    masks = [r for r in regs if r.kind == "redact"]
    assert {m.name for m in masks} >= {"mask", "auto chat mask"}          # manual mask kept, chat mask added
    h.cfg.perception.auto_mask_chat = False
    h.run(4)
    assert {m.name for m in h.mon.effective_regions() if m.kind == "redact"} == {"mask"}


def test_profile_click_requires_located_control(auto, monkeypatch):
    h = auto
    clicks = []

    class Interactor:
        def click(self, *a, **k): clicks.append(a); return True
        def move(self, *a, **k): return True
        def idle_seconds(self): return 10.0
        def key(self, *a, **k): return True
    h.mon.interactor = Interactor()
    h.mon._inline_lookup = True
    # located with confidence -> allowed
    h.run(6)
    region, allowed, why = h.mon._profile_target()
    assert allowed and region is not None and region.kind == "profile"
    # low confidence -> refused, no click
    h.cfg.perception.profile_min_confidence = 0.99
    region, allowed, why = h.mon._profile_target()
    assert not allowed and "no click" in why
    assert h.mon.request_account_lookup() is False and clicks == []
    assert h.mon.account.status == "FAILED" and "not located" in h.mon.account.error


def test_relocalizes_after_restart_and_resize(auto):
    h = auto
    h.run(6)
    n = h.mon.perception.status.discoveries
    h.close_studio()                                                      # Studio exits ...
    h.run(30)
    h.sys.add(make_window(hwnd=0x1002, pid=9999, rect=Rect(100, 100, 100 + W, 100 + H)))   # ... and restarts with a new pid
    h.run(20)
    assert h.mon.perception.status.discoveries >= n + 1 and "restarted" in h.mon.perception.status.last_reason
    # resize: the capture size changes -> rediscovery with the new size
    m = h.mon.perception.status.discoveries
    h.scene_cap.set_scene(width=1024, height=600)
    h.sys.windows[0x1002] = make_window(hwnd=0x1002, pid=9999, rect=Rect(100, 100, 1124, 700))
    h.run(10)
    assert h.mon.perception.status.discoveries >= m + 1 and "resized" in h.mon.perception.status.last_reason


def test_audio_worker_status_and_silence_alert_only_while_live(cfg, rules, clock, tmp_path):
    cfg.perception.enabled = True
    cfg.regions = []
    sessions = [AudioSession(4242, "TikTok LIVE Studio.exe", True)]
    resolver = AudioSourceResolver(sessions=lambda: sessions, endpoints=lambda: [AudioEndpoint("Mic", True, True)], loopback_supported=True)
    captures = []

    class Cap:
        def __init__(self, pid, cb): self.cb, self.error, self.started = cb, "", False; captures.append(self)
        def start(self): self.started = True
        def stop(self): self.started = False
    worker = AudioWorker(resolver, AudioAnalyzer(AudioConfig(silence_seconds=4, recover_seconds=2), 16000, vad=EnergyVad(), mono=clock),
                         loopback_factory=Cap, mono=clock, rebind_interval=0)
    h = AutoHarness(cfg, rules, clock, tmp_path, audio=worker)
    h.run(6)
    assert h.mon.activity.audio["binding"]["kind"] == KIND_LOOPBACK and captures and captures[0].started
    silence = bytes(3200)
    for _ in range(5):                                                    # silence before going live: no alert
        captures[0].cb(silence); h.run(2)
    assert not [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STRAUD")]
    h.go_live()
    for _ in range(6):
        captures[0].cb(silence); h.run(2)
    alerts = [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STRAUD") and d["event_id"].count("-") == 3]
    assert len(alerts) == 1 and "Studio rendered audio" in alerts[0]["payload"]["text"]
    assert h.mon.activity.stream["AUDIO_SILENCE"]["state"] == "DISABLED"   # visual meter not used while a real source is bound
    import numpy as np
    tone = (0.3 * np.sin(np.arange(1600) / 8.0) * 32767).astype("int16").tobytes()
    for _ in range(4):
        captures[0].cb(tone); h.run(2)
    assert [d for d in all_deliveries(h.queue) if d["event_id"].startswith("STRAUD") and d["event_id"].endswith("-RES")]


def test_visual_meter_fallback_when_no_audio_source(auto):
    h = auto
    resolver = AudioSourceResolver(sessions=lambda: [], endpoints=lambda: [], loopback_supported=False)
    h.mon.audio_worker = AudioWorker(resolver, AudioAnalyzer(AudioConfig(), 16000, vad=EnergyVad(), mono=h.clock), mono=h.clock, rebind_interval=0)
    h.run(6)
    assert h.mon.activity.audio["binding"]["kind"] == KIND_VISUAL
    assert "audio" in {r.kind for r in h.mon.effective_regions()}          # the discovered on-screen meter stays in use
