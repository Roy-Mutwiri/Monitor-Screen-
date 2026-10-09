"""Capture binding/backends, process identity, health debounce, broadcast-start
events. All synthetic: fake window system, fake frames, fake WGC session."""
from pathlib import Path

import pytest
from PIL import Image

from conftest import (TOKEN_A, TOKEN_B, FakeCapturer, FakeClock, FakeOcr, FakeTransport, FakeWindowSystem,
                      all_deliveries, fake_frame, make_window)
from test_activity import LIVE_TEXT, LOADING_TEXT, NOT_LIVE_TEXT, Harness

from studio_monitor import app as appmod
from studio_monitor.bots import CAT_BROADCAST, CAT_HEALTH, CAT_RESTRICTIONS, BotRegistry, EVENT_CATEGORIES
from studio_monitor.broadcast import LiveState
from studio_monitor.broadcast_events import BroadcastEpisodeTracker
from studio_monitor.config import AppConfig, TargetIdentity
from studio_monitor.credentials import MemoryCredentialStore
from studio_monitor.health import HealthAlertPolicy
from studio_monitor.monitor import EVT_ALREADY_LIVE, EVT_BROADCAST_STARTED
from studio_monitor.queue import DeliveryQueue
from studio_monitor.regions import Region
from studio_monitor.target import identity_from_window, related_windows, same_process, validate_handle
from studio_monitor.win32 import capture as capmod
from studio_monitor.win32.capture import (BACKEND_DESKTOP, BACKEND_PRINTWINDOW, BACKEND_WGC, CaptureService,
                                          SyncFrameService, is_blank, window_visible_at_rect)
from studio_monitor.win32.windows import Rect


# ---------------------------------------------------------------- frame validity

def test_blank_vs_static_frames():
    assert is_blank(Image.new("RGB", (300, 200), (0, 0, 0)))
    assert is_blank(Image.new("RGB", (300, 200), (255, 255, 255)))
    assert not is_blank(fake_frame(300, 200))
    assert not is_blank(fake_frame(300, 200))        # identical static content is still valid


# ---------------------------------------------------------------- CaptureService with a fake WGC session

class FakeSession:
    """Stands in for WgcSession: scripted frames, liveness and size."""
    instances: list = []

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.frames = 0
        self.closed = False
        self.error = ""
        self._alive = True
        self._latest = None
        self.started = False
        FakeSession.instances.append(self)

    @staticmethod
    def available():
        return True

    def start(self):
        self.started = True
        self.deliver(fake_frame(640, 360))            # a new session always yields a frame

    def deliver(self, img):
        self._latest = (img, 1.0, 1.0)
        self.frames += 1

    @property
    def alive(self):
        return self._alive and not self.closed

    def latest(self):
        return self._latest

    def stop(self):
        self.closed = True


@pytest.fixture
def svc(monkeypatch, clock):
    FakeSession.instances.clear()
    monkeypatch.setattr(capmod, "WgcSession", FakeSession)
    monkeypatch.setattr(capmod, "print_window", lambda hwnd: None)        # PrintWindow would be blank
    monkeypatch.setattr(capmod, "screen_crop", lambda r: fake_frame(r.width, r.height, (9, 9, 9)))
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    sys_.foreground = 0x9999                                               # an unrelated window is in front
    service = CaptureService(sys_, interval=0.1, max_age=30, refresh_interval=15, clock=clock, mono=clock)
    service.wgc_available = True
    service._hwnd = 0x1001                                                 # bind without starting the thread
    service._status.hwnd = 0x1001
    return service, sys_


def test_wgc_capture_ignores_foreground_and_static_content(svc, clock):
    service, sys_ = svc
    service._tick()
    st = service.status()
    assert st.health == "OK" and st.backend == BACKEND_WGC and service.frame() is not None
    # static window: no new frames; still OK, still fresh, no restart within refresh interval
    for _ in range(5):
        clock.advance(2); service._tick()
    assert service.status().health == "OK" and service.status().session_restarts == 0
    assert service.frame() is not None                                     # same frame, still current
    # heartbeat: after the refresh interval with no new frame the session is recreated
    clock.advance(16); service._tick()
    assert service.status().session_restarts == 1 and len(FakeSession.instances) == 2
    assert service.status().health == "OK"


def test_wgc_recreated_on_resize_and_device_loss(svc, clock):
    service, sys_ = svc
    service._tick()
    first = FakeSession.instances[-1]
    sys_.windows[0x1001] = make_window(rect=Rect(0, 0, 800, 500))        # resized
    service._tick()
    assert FakeSession.instances[-1] is not first and service.status().session_restarts == 1
    sess = FakeSession.instances[-1]
    sess._alive = False; sess.error = "device removed"                   # graphics device lost
    service._tick()
    assert FakeSession.instances[-1] is not sess and service.status().session_restarts == 2
    assert service.status().health == "OK"


def test_blank_wgc_frames_are_not_accepted(svc, clock):
    service, sys_ = svc
    service._tick()
    sess = FakeSession.instances[-1]
    sess.deliver(Image.new("RGB", (640, 360), (0, 0, 0)))
    clock.advance(1); service._tick()
    assert service.status().health == "DEGRADED" and service.status().code == "blank"
    assert service.frame().seq == 1                                        # last *valid* frame kept, not the black one


def test_minimized_locked_closed_reasons(svc, clock):
    service, sys_ = svc
    sys_.windows[0x1001] = make_window(minimized=True)
    service._tick()
    assert service.status().code == "minimized" and "minimized" in service.status().reason
    sys_.windows[0x1001] = make_window()
    sys_.locked = True
    service._tick()
    assert service.status().code == "desktop_locked"
    sys_.locked = False
    sys_.remove(0x1001)
    service._tick()
    assert service.status().code == "target_closed"


def test_desktop_fallback_is_explicit_and_visibility_checked(monkeypatch, clock):
    monkeypatch.setattr(capmod, "print_window", lambda hwnd: None)
    monkeypatch.setattr(capmod, "screen_crop", lambda r: fake_frame(r.width, r.height, (9, 9, 9)))
    sys_ = FakeWindowSystem()
    win = sys_.add(make_window())
    service = CaptureService(sys_, interval=0.1, prefer="printwindow", clock=clock, mono=clock)
    service.wgc_available = False
    service._hwnd = 0x1001; service._status.hwnd = 0x1001
    sys_.foreground = 0x9999                                               # covered / not in front
    service._tick()
    assert service.frame() is None and service.status().code == "fallback_unavailable"
    assert "another window" in service.status().reason
    sys_.foreground = 0x1001
    sys_.covering[0x1001] = 0x7777                                         # foreground but something overlaps
    service._tick()
    assert service.frame() is None
    sys_.covering.clear()
    service._tick()
    assert service.frame() is not None and service.status().backend == BACKEND_DESKTOP
    assert service.status().code == "fallback" and "fallback" in service.status().reason.lower()
    assert window_visible_at_rect(sys_, win) == (True, "")


def test_service_rebinds_and_releases_on_target_change(svc, clock):
    service, sys_ = svc
    service._tick()
    old = FakeSession.instances[-1]
    sys_.add(make_window(hwnd=0x2222, pid=4242))
    service.bind(0x2222)
    assert old.closed and service.frame() is None and service.status().hwnd == 0x2222
    service._stop.set(); service._thread.join(timeout=3)
    service._tick()
    assert service.frame().hwnd == 0x2222
    service.stop()
    assert FakeSession.instances[-1].closed and service.status().hwnd == 0


# ---------------------------------------------------------------- target identity

def test_pid_reuse_is_rejected_by_creation_time(studio_identity):
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    sys_.start_times[4242] = 5000.0
    ident = identity_from_window(sys_.get_window(0x1001), sys_)
    assert ident.process_start == 5000.0 and validate_handle(sys_, ident).ok
    sys_.start_times[4242] = 9000.0                                        # same pid, different (new) process
    r = validate_handle(sys_, ident)
    assert not r.ok and "reused" in r.reason
    assert not same_process(sys_, ident, 4242) and same_process(sys_, TargetIdentity(pid=4242), 4242)


def test_hwnd_recreation_rebinds_frames(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.run(6)
    assert h.mon.frames.status().hwnd == 0x1001
    h.sys.remove(0x1001)
    h.sys.add(make_window(hwnd=0x3333, pid=4242))
    h.run(6)
    assert h.cfg.target.hwnd == 0x3333 and h.mon.frames.status().hwnd == 0x3333
    assert h.mon.health.capture == "OK"


def test_dialog_capture_limited_to_owned_or_sibling_windows():
    sys_ = FakeWindowSystem()
    main = sys_.add(make_window())
    sys_.add(make_window(hwnd=0x5001, title="Notice", pid=4242, rect=Rect(300, 300, 800, 600)))             # sibling
    sys_.add(make_window(hwnd=0x5002, title="Helper", pid=4300, rect=Rect(300, 300, 800, 600)))             # child process
    sys_.add(make_window(hwnd=0x5003, title="Owned", pid=999, owner=0x1001, rect=Rect(0, 0, 500, 400)))     # owned
    sys_.add(make_window(hwnd=0x5004, title="OtherClass", pid=4242, cls="Other", rect=Rect(0, 0, 500, 400)))
    sys_.children[4242] = {4300}
    assert {w.hwnd for w in related_windows(sys_, main)} == {0x5001, 0x5003}


# ---------------------------------------------------------------- monitor: binding and health

def test_target_binding_survives_foreground_changes_and_health_ok(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    for fg in (0x1001, 0x9999, 0x8888, 0x1001):
        h.sys.foreground = fg
        h.run(4)
        assert h.mon.health.capture == "OK" and h.mon.health.capture_reason == ""
    assert h.alerts("status") == []                                        # no health flapping
    assert all(d["kind"] != "status" for d in all_deliveries(h.queue))


def test_unrelated_window_frames_are_never_evidence(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.run(4)
    other = make_window(hwnd=0x4444, title="Chrome", exe="C:/chrome.exe", pid=77)
    h.sys.add(other)
    # a frame tagged with a different hwnd must be ignored by the monitor
    frame = h.mon.frames.frame()
    assert frame.hwnd == 0x1001
    rogue = FakeCapturer().capture(other)
    rogue.seq = 999
    h.mon.frames.frame = lambda max_age=None: rogue
    h.ocr.default = "Your LIVE has been restricted for violating our Community Guidelines"
    h.run(10)
    assert h.alerts("incident") == []                                      # nothing from Chrome's pixels


def test_capture_degraded_only_for_real_reasons(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.run(4)
    h.sys.windows[0x1001] = make_window(minimized=True)
    h.run(4)
    assert h.mon.health.capture == "DEGRADED" and "minimized" in h.mon.health.capture_reason
    h.sys.windows[0x1001] = make_window()
    h.cap.fail_hwnds.add(0x1001)
    h.run(4)
    assert h.mon.health.capture == "DEGRADED" and "empty" in h.mon.health.capture_reason.lower()
    h.cap.fail_hwnds.clear()
    h.run(4)
    assert h.mon.health.capture == "OK"


# ---------------------------------------------------------------- health debounce

def test_health_alert_debounce_and_recovery_pairing(cfg, rules, clock):
    cfg.health.degrade_after_seconds = 15
    cfg.health.recover_after_seconds = 10
    cfg.activity.notify_already_running = False
    h = Harness(cfg, rules, clock)
    h.run(4)
    h.sys.windows[0x1001] = make_window(minimized=True)
    h.run(10)                                                              # short blip (< 15 s)
    h.sys.windows[0x1001] = make_window()
    h.run(30)
    assert [d for d in all_deliveries(h.queue) if d["kind"] == "status"] == []
    h.sys.windows[0x1001] = make_window(minimized=True)
    h.run(20)                                                              # persists > 15 s -> one degraded alert
    h.sys.windows[0x1001] = make_window(cloaked=True)                      # cause changes within the episode
    h.run(20)
    status = [d for d in all_deliveries(h.queue) if d["kind"] == "status"]
    assert len(status) == 1 and "DEGRADED" in status[0]["payload"]["text"] and "minimized" in status[0]["payload"]["text"]
    h.sys.windows[0x1001] = make_window()
    h.run(6)                                                               # not yet stable for 10 s
    assert len([d for d in all_deliveries(h.queue) if d["kind"] == "status"]) == 1
    h.run(10)
    status = [d for d in all_deliveries(h.queue) if d["kind"] == "status"]
    assert len(status) == 2 and "RECOVERED" in status[1]["payload"]["text"]
    h.run(60)
    assert len([d for d in all_deliveries(h.queue) if d["kind"] == "status"]) == 2


def test_health_policy_persists_episode_across_restart(tmp_path, clock):
    q = DeliveryQueue(tmp_path / "q.sqlite3", clock=clock)
    p = HealthAlertPolicy(q, degrade_after=15, recover_after=10, clock=clock, mono=clock)
    assert p.update(True, "Studio is minimized") is None
    clock.advance(16)
    a = p.update(True, "Studio is minimized")
    assert a and a.kind == "degraded"
    p2 = HealthAlertPolicy(q, degrade_after=15, recover_after=10, clock=clock, mono=clock)   # monitor restarted
    clock.advance(60)
    assert p2.update(True, "Studio is minimized") is None                  # no re-alert for the same episode
    assert p2.update(False, "") is None
    clock.advance(11)
    r = p2.update(False, "")
    assert r and r.kind == "recovered"
    p3 = HealthAlertPolicy(q, degrade_after=15, recover_after=10, clock=clock, mono=clock)
    assert p3.update(False, "") is None                                    # clean state, no spurious recovery


def test_restriction_alerts_bypass_health_debounce(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.ocr.default = "Your LIVE has been restricted for violating our Community Guidelines"
    h.run(2)
    assert len(h.alerts("incident")) == 1


# ---------------------------------------------------------------- broadcast-start events

def broadcast_events(h):
    return [d for d in all_deliveries(h.queue) if "GONE LIVE" in d["payload"]["caption"] or "ALREADY LIVE" in d["payload"]["caption"]]


def test_not_live_to_live_generates_one_screenshot_event(cfg, rules, clock):
    cfg.account_label = "@creator"
    h = Harness(cfg, rules, clock)
    h.run(60, step=10)
    assert h.mon.broadcast.confirmed == LiveState.NOT_LIVE and broadcast_events(h) == []
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = broadcast_events(h)
    assert len(ev) == 1
    cap = ev[0]["payload"]["caption"]
    assert cap.startswith("\U0001F534 <b>test-pc\u2019s Live \u2014 HAS GONE LIVE</b>") and "PC: test-pc" in cap
    assert "TikTok LIVE Studio is broadcasting." in cap
    assert "Detected at:" in cap and "Status: LIVE" in cap and "Account: @creator" in cap
    assert "gap" not in cap and Path(ev[0]["screenshot_path"]).exists()
    assert h.event_types()[-1] == EVT_BROADCAST_STARTED
    h.run(10 * 60, step=10)
    assert len(broadcast_events(h)) == 1                                   # stays one per broadcast


def test_initial_live_gets_already_live_wording(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = broadcast_events(h)
    assert len(ev) == 1 and "ALREADY LIVE" in ev[0]["payload"]["caption"]
    assert "not a newly observed broadcast start" in ev[0]["payload"]["caption"]
    assert "GONE LIVE" not in ev[0]["payload"]["caption"] and h.event_types()[-1] == EVT_ALREADY_LIVE


def test_unknown_to_live_recovery_does_not_duplicate(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    assert len(broadcast_events(h)) == 1
    h.ocr.default = LOADING_TEXT                                           # UNKNOWN gap
    h.run(5 * 60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    assert len(broadcast_events(h)) == 1                                   # same broadcast resumed
    h.ocr.default = NOT_LIVE_TEXT                                          # confirmed NOT_LIVE re-arms
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    assert len(broadcast_events(h)) == 2


def test_live_after_unknown_gap_is_described_honestly(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.run(60, step=10)                                                     # NOT_LIVE
    h.ocr.default = LOADING_TEXT
    h.run(5 * 60, step=10)                                                 # UNKNOWN for 5 minutes
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = broadcast_events(h)
    assert len(ev) == 1 and "after a monitoring gap" in ev[0]["payload"]["caption"]
    assert "exact start time was not observed" in ev[0]["payload"]["caption"]


def test_restart_preserves_broadcast_episode_dedup(cfg, rules, clock):
    h1 = Harness(cfg, rules, clock)
    h1.run(60, step=10)
    h1.ocr.default = LIVE_TEXT
    h1.run(60, step=10)
    assert len(broadcast_events(h1)) == 1
    episode = h1.mon.episodes.state.episode_id
    h2 = Harness(cfg, rules, clock, sys_=h1.sys, queue=h1.queue)           # monitor restart while still live
    h2.ocr.default = LIVE_TEXT
    h2.run(60, step=10)
    ev = broadcast_events(h2)
    assert len(ev) == 2 and "ALREADY LIVE" in ev[1]["payload"]["caption"]
    assert h2.mon.episodes.state.episode_id == episode                     # same broadcast, no "gone live"


def test_going_live_cancels_queued_offline_reminder(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.run(61 * 60, step=10)
    rem = h.alerts("reminder")
    assert len(rem) == 1 and rem[0]["status"] == "pending"
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    assert h.alerts("reminder")[0]["status"] == "cancelled"
    assert not h.mon.reminders.state.active and len(broadcast_events(h)) == 1


def test_broadcast_screenshot_is_the_confirming_frame_and_masked(cfg, rules, clock):
    cfg.regions = [Region("chat", 0.5, 0.0, 0.5, 1.0, "redact")]
    h = Harness(cfg, rules, clock)
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = broadcast_events(h)[0]
    img = Image.open(ev["screenshot_path"])
    assert img.getpixel((img.width - 1, 0)) == (0, 0, 0) and img.getpixel((0, 0)) == (40, 40, 40)
    assert img.size == (1280, 720)


def test_broadcast_subscription_migration_and_multi_bot_delivery(cfg, rules, clock, tmp_path):
    path = tmp_path / "config.json"
    store = MemoryCredentialStore()
    reg = BotRegistry(cfg, store, save=lambda: cfg.save(path))
    a = reg.add("A", TOKEN_A, "1", subscriptions=[CAT_RESTRICTIONS])
    b = reg.add("B", TOKEN_B, "2", subscriptions=[CAT_RESTRICTIONS], enabled=False)
    cfg.config_version = 3                                                 # settings written before this feature
    queue = DeliveryQueue(cfg.db_path, clock=clock)
    notes = appmod.run_migrations(cfg, path, reg, queue)
    assert any("Broadcast started" in n for n in notes)
    assert CAT_BROADCAST in a.subscriptions and CAT_BROADCAST not in b.subscriptions
    assert AppConfig.load(path).config_version == 6
    assert appmod.run_migrations(cfg, path, reg, queue) == [] or all("Broadcast" not in n for n in appmod.run_migrations(cfg, path, reg, queue))
    reg.set_enabled(b.bot_id, True)
    h = Harness(cfg, rules, clock, queue=queue)
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = [d for d in all_deliveries(h.queue) if "GONE LIVE" in d["payload"]["caption"]]
    assert [d["bot_name"] for d in ev] == ["A"]                            # B not subscribed


def test_episode_tracker_unit(tmp_path, clock):
    q = DeliveryQueue(tmp_path / "q.sqlite3", clock=clock)
    t = BroadcastEpisodeTracker(q, max_gap_seconds=30, clock=clock, mono=clock)
    assert t.observe(LiveState.UNKNOWN, True) is None
    assert t.observe(LiveState.NOT_LIVE, True) is None
    ev = t.observe(LiveState.LIVE, True)
    assert ev.kind == "started"
    assert t.observe(LiveState.LIVE, True) is None and t.observe(LiveState.LIVE, False) is None
    assert t.observe(LiveState.NOT_LIVE, True).kind == "ended"
    clock.advance(120)                                                     # a gap after NOT_LIVE
    assert t.observe(LiveState.LIVE, True).kind == "started_after_gap"
    t2 = BroadcastEpisodeTracker(q, max_gap_seconds=30, clock=clock, mono=clock)
    assert t2.observe(LiveState.LIVE, True).kind == "already_live" and t2.state.episode_id == t.state.episode_id
