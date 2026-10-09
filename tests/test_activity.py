"""Studio opened/closed notifications and not-live reminders, end to end with
fakes (window system, capture, OCR) and a controllable clock. No network."""
from pathlib import Path

import pytest
from PIL import Image

from conftest import TOKEN_A, FakeCapturer, FakeClock, FakeOcr, FakeWindowSystem, all_deliveries, make_window

from studio_monitor.alerts import local_ts
from studio_monitor.broadcast import LiveRules, LiveState
from studio_monitor.config import AppConfig
from studio_monitor.monitor import Monitor
from studio_monitor.queue import DeliveryQueue
from studio_monitor.regions import Region
from studio_monitor.reminders import EVT_REMINDER_CANCELLED
from studio_monitor.sessions import EVT_ALREADY_RUNNING, EVT_CLOSED, EVT_OPENED
from studio_monitor.win32.windows import Rect

RULES = Path(__file__).resolve().parents[1] / "rules" / "live_state_rules.json"
NOT_LIVE_TEXT = "Scenes  Sources  Go LIVE  Preview  Add a title  Chat"
LIVE_TEXT = "Scenes  Sources  LIVE 00:12:34  1,204 viewers  End LIVE  Chat"
LOADING_TEXT = "Loading... please wait"
POPUP_TEXT = "Your LIVE has been restricted for violating our Community Guidelines"


class Harness:
    def __init__(self, cfg, rules, clock, studio_running=True, sys_=None, queue=None):
        self.cfg, self.rules, self.clock = cfg, rules, clock
        self.sys = sys_ or FakeWindowSystem()
        if studio_running and sys_ is None:
            self.sys.add(make_window())
        self.cap = FakeCapturer()
        self.ocr = FakeOcr()
        self.ocr.default = NOT_LIVE_TEXT
        self.queue = queue or DeliveryQueue(cfg.db_path, clock=clock)
        from studio_monitor.bots import BotRegistry
        from studio_monitor.credentials import MemoryCredentialStore
        self.registry = BotRegistry(cfg, MemoryCredentialStore(), save=lambda: None, queue=self.queue)
        if not cfg.bots:
            self.registry.add("Default Bot", TOKEN_A, "42")
        self.events, self.statuses, self.activity = [], [], []
        self.mon = Monitor(cfg, self.sys, self.cap, self.ocr, rules, self.queue, self.registry, None, clock,
                           on_event=self.events.append, on_status=self.statuses.append,
                           live_rules=LiveRules.load(RULES), mono=clock, on_activity=self.activity.append)

    def run(self, seconds, step=2.0):
        n = int(seconds / step)
        for _ in range(n):
            self.mon.tick()
            self.clock.advance(step)

    def alerts(self, kind):
        return [a for a in self._all_alerts() if a["kind"] == kind]

    def _all_alerts(self):
        return all_deliveries(self.queue)

    def event_types(self):
        return [e["event_type"] for e in reversed(self.queue.recent_events(100))]

    def close_studio(self):
        self.sys.remove(0x1001)
        self.sys.alive.clear()


@pytest.fixture
def h(cfg, rules, clock):
    cfg.activity.close_debounce_seconds = 6
    cfg.activity.open_screenshot_timeout_seconds = 10
    return Harness(cfg, rules, clock)


# ---------------------------------------------------------------- opened / closed

def test_studio_opens_exactly_one_notification(cfg, rules, clock):
    h = Harness(cfg, rules, clock, studio_running=False)
    h.run(10)
    assert h.alerts("activity") == []
    h.sys.add(make_window())
    h.run(20)
    opened = [a for a in h.alerts("activity") if "STUDIO OPENED" in a["payload"]["caption"]]
    assert len(opened) == 1
    cap = opened[0]["payload"]["caption"]
    assert "PC: test-pc" in cap and "is now running" in cap and "UTC" in cap
    assert Path(opened[0]["screenshot_path"]).exists()
    assert h.event_types().count(EVT_OPENED) == 1 and EVT_ALREADY_RUNNING not in h.event_types()


def test_opened_without_screenshot_after_timeout(cfg, rules, clock):
    cfg.activity.open_screenshot_timeout_seconds = 8
    h = Harness(cfg, rules, clock, studio_running=False)
    h.run(4)
    h.sys.add(make_window())
    h.cap.fail_hwnds.add(0x1001)
    h.run(6)
    assert h.alerts("activity") == []          # waiting for a usable screenshot
    h.run(6)
    opened = h.alerts("activity")
    assert len(opened) == 1 and opened[0]["screenshot_path"] == ""
    assert "Screenshot unavailable" in opened[0]["payload"]["text"]


def test_already_running_at_startup_is_distinct(h):
    h.run(10)
    acts = h.alerts("activity")
    assert len(acts) == 1
    assert "ALREADY RUNNING" in acts[0]["payload"]["caption"]
    assert "monitoring started" in acts[0]["payload"]["caption"]
    assert h.event_types() == [EVT_ALREADY_RUNNING]


def test_already_running_notification_can_be_disabled(cfg, rules, clock):
    cfg.activity.notify_already_running = False
    h = Harness(cfg, rules, clock)
    h.run(10)
    assert h.alerts("activity") == [] and h.mon.sessions.running


def test_dialog_and_window_recreation_do_not_open_new_session(h):
    h.run(6)
    h.sys.add(make_window(hwnd=0x7007, title="Notice", pid=4242, rect=Rect(400, 300, 900, 600)))
    h.run(6)
    # window recreated by the same process (new hwnd, same pid)
    h.sys.remove(0x1001)
    h.sys.add(make_window(hwnd=0x2222, pid=4242))
    h.run(10)
    assert h.mon.sessions.state.session.pid == 4242
    assert h.event_types() == [EVT_ALREADY_RUNNING]
    assert h.cfg.target.hwnd == 0x2222


def test_minimize_and_capture_loss_do_not_close(h):
    h.run(4)
    h.sys.windows[0x1001] = make_window(minimized=True)
    h.run(30)
    h.sys.windows[0x1001] = make_window()
    h.cap.fail_hwnds.add(0x1001)
    h.run(30)
    h.sys.windows[0x1001] = make_window(cloaked=True)   # locked desktop / hidden
    h.run(30)
    assert EVT_CLOSED not in h.event_types()
    assert h.mon.sessions.state.app_state == "RUNNING"


def test_window_lost_but_process_alive_does_not_close(h):
    h.run(4)
    h.sys.remove(0x1001)          # window gone, process still alive
    h.run(30)
    assert EVT_CLOSED not in h.event_types() and h.mon.sessions.state.session is not None


def test_confirmed_process_exit_exactly_one_closed(h):
    h.run(4)
    h.close_studio()
    h.run(4)
    assert EVT_CLOSED not in h.event_types()  # inside debounce
    h.run(20)
    assert h.event_types().count(EVT_CLOSED) == 1
    closed = [a for a in h.alerts("activity") if "STUDIO CLOSED" in a["payload"]["caption"]]
    assert len(closed) == 1 and "has closed" in closed[0]["payload"]["caption"]
    assert h.mon.sessions.state.app_state == "NOT_RUNNING"


def test_closure_screenshot_keeps_original_capture_time(h):
    h.run(4)                                   # frames captured up to t0
    last_capture_ts = h.clock.now - 2          # last tick before closure
    h.close_studio()
    h.run(30)
    closed = [a for a in h.alerts("activity") if "STUDIO CLOSED" in a["payload"]["caption"]][0]
    cap = closed["payload"]["caption"]
    assert "last available screenshot before closure" in cap
    assert f"Screenshot captured: {local_ts(last_capture_ts)}" in cap
    assert local_ts(h.clock.now - 2) not in cap.split("Screenshot captured:")[1]
    assert Path(closed["screenshot_path"]).exists()


def test_closed_text_only_when_no_frame(cfg, rules, clock):
    cfg.activity.close_debounce_seconds = 4
    h = Harness(cfg, rules, clock, studio_running=False)
    h.run(2)
    h.sys.add(make_window())
    h.cap.fail_hwnds.add(0x1001)               # never a valid frame
    h.run(30)
    h.close_studio()
    h.run(20)
    closed = [a for a in h.alerts("activity") if "STUDIO CLOSED" in a["payload"]["caption"]]
    assert len(closed) == 1 and closed[0]["screenshot_path"] == ""
    assert "No screenshot available" in closed[0]["payload"]["text"]


def test_restart_is_closed_then_opened(h):
    h.run(4)
    first = h.mon.sessions.session_id
    h.close_studio()
    h.run(2)
    h.sys.add(make_window(hwnd=0x9009, pid=8080))   # new process before debounce elapsed
    h.run(10)
    types = h.event_types()
    assert types == [EVT_ALREADY_RUNNING, EVT_CLOSED, EVT_OPENED]
    assert h.mon.sessions.session_id != first and h.mon.sessions.state.session.pid == 8080


def test_notifications_can_be_disabled(cfg, rules, clock):
    cfg.activity.notify_opened = False
    cfg.activity.notify_closed = False
    cfg.activity.notify_already_running = False
    cfg.activity.close_debounce_seconds = 4
    h = Harness(cfg, rules, clock, studio_running=False)
    h.run(2); h.sys.add(make_window()); h.run(6); h.close_studio(); h.run(20)
    assert h.alerts("activity") == []
    assert h.event_types() == [EVT_OPENED, EVT_CLOSED]   # still recorded in history


# ---------------------------------------------------------------- broadcast state + reminders

def test_not_live_confirmed_and_below_threshold_no_reminder(h):
    h.run(30 * 60, step=10)
    assert h.mon.broadcast.confirmed == LiveState.NOT_LIVE
    assert h.alerts("reminder") == []
    snap = h.activity[-1]
    assert 0 < snap.offline_seconds < 31 * 60 and snap.remaining_seconds > 0 and snap.accumulating


def test_threshold_reached_exactly_one_reminder(h):
    h.run(61 * 60, step=10)
    rem = h.alerts("reminder")
    assert len(rem) == 1
    cap = rem[0]["payload"]["caption"]
    assert cap.startswith("<b>TIME TO GO LIVE</b>") and "PC: test-pc" in cap
    assert "confirmed not live for at least 1 hour" in cap and "go live when ready" in cap
    assert "unverified" in cap                      # seeded rules not calibrated
    assert Path(rem[0]["screenshot_path"]).exists()  # fresh frame attached
    h.run(60 * 60, step=10)
    assert len(h.alerts("reminder")) == 1            # repeats disabled by default
    assert h.mon.reminders.state.reminders_sent == 1


def test_repeat_reminders(cfg, rules, clock):
    cfg.activity.repeat_enabled = True
    cfg.activity.repeat_interval_minutes = 30
    cfg.activity.repeat_max_count = 2
    h = Harness(cfg, rules, clock)
    h.run(3 * 60 * 60, step=10)
    seqs = [a["payload"]["caption"].count("Repeat reminder") for a in h.alerts("reminder")]
    assert len(seqs) == 3 and seqs == [0, 1, 1]


def test_live_resets_offline_episode(h):
    h.run(30 * 60, step=10)
    ep1 = h.mon.reminders.state.episode_id
    assert ep1
    h.ocr.default = LIVE_TEXT
    h.run(60)
    assert h.mon.broadcast.confirmed == LiveState.LIVE
    assert not h.mon.reminders.state.active and h.mon.reminders.state.accumulated_seconds == 0
    h.ocr.default = NOT_LIVE_TEXT
    h.run(40 * 60, step=10)
    assert h.mon.reminders.state.episode_id not in ("", ep1)
    assert h.alerts("reminder") == []                # new episode started from zero


def test_unknown_does_not_accumulate(h):
    h.run(10 * 60, step=10)
    before = h.mon.reminders.state.accumulated_seconds
    h.ocr.default = LOADING_TEXT
    h.run(50 * 60, step=10)
    assert h.mon.broadcast.confirmed == LiveState.UNKNOWN
    assert h.mon.reminders.state.accumulated_seconds == before
    assert h.alerts("reminder") == []
    h.ocr.default = NOT_LIVE_TEXT
    h.run(2 * 60, step=10)
    assert h.mon.reminders.state.accumulated_seconds == pytest.approx(before + 2 * 60 - 30, abs=12)


def test_popup_on_main_window_is_not_live_evidence(h):
    h.run(2 * 60, step=10)
    h.ocr.default = POPUP_TEXT
    h.run(60, step=10)
    assert h.mon.broadcast.confirmed == LiveState.UNKNOWN
    assert len(h.alerts("incident")) == 1


def test_sleep_gap_does_not_inflate_offline_time(h):
    h.run(10 * 60, step=10)
    before = h.mon.reminders.state.accumulated_seconds
    h.clock.advance(2 * 60 * 60)         # machine asleep / locked: no observations
    for _ in range(3):                   # the gap broke the streak: three fresh observations re-confirm
        h.mon.tick(); h.clock.advance(10)
    assert h.mon.reminders.state.accumulated_seconds == before   # nothing counted across the gap
    h.mon.tick(); h.clock.advance(10)
    assert h.mon.reminders.state.accumulated_seconds == pytest.approx(before + 10, abs=1)
    assert h.alerts("reminder") == []


def test_monitor_restart_preserves_duration_without_counting_downtime(cfg, rules, clock):
    h1 = Harness(cfg, rules, clock)
    h1.run(30 * 60, step=10)
    acc = h1.mon.reminders.state.accumulated_seconds
    ep = h1.mon.reminders.state.episode_id
    clock.advance(20 * 60)               # monitor stopped for 20 minutes
    h2 = Harness(cfg, rules, clock, sys_=h1.sys, queue=h1.queue)
    assert h2.mon.reminders.state.episode_id == ep
    assert h2.mon.reminders.state.accumulated_seconds == acc
    h2.run(60, step=10)                  # needs fresh confirmation (3 obs) before resuming
    assert h2.mon.reminders.state.accumulated_seconds == pytest.approx(acc + 60 - 30, abs=12)
    assert h2.alerts("reminder") == []


def test_monitor_restart_does_not_duplicate_enqueued_reminder(cfg, rules, clock):
    h1 = Harness(cfg, rules, clock)
    h1.run(61 * 60, step=10)
    assert len(h1.alerts("reminder")) == 1
    h2 = Harness(cfg, rules, clock, sys_=h1.sys, queue=h1.queue)
    h2.run(10 * 60, step=10)
    assert len(h2.alerts("reminder")) == 1 and h2.mon.reminders.state.reminders_sent == 1


def test_going_live_cancels_unsent_reminder(h):
    h.run(61 * 60, step=10)
    rem = h.alerts("reminder")[0]
    assert rem["status"] == "pending"
    h.ocr.default = LIVE_TEXT
    h.run(60)
    rem = h.alerts("reminder")[0]
    assert rem["status"] == "cancelled" and "live" in rem["last_error"]
    assert EVT_REMINDER_CANCELLED in h.event_types()


def test_studio_closure_ends_episode_and_cancels_pending(h):
    h.run(61 * 60, step=10)
    h.close_studio()
    h.run(30)
    rem = h.alerts("reminder")[0]
    assert rem["status"] == "cancelled" and "closed" in rem["last_error"]
    assert not h.mon.reminders.state.active
    assert h.event_types()[-2:] in ([EVT_CLOSED, EVT_REMINDER_CANCELLED], [EVT_REMINDER_CANCELLED, EVT_CLOSED])


def test_reminders_can_be_disabled(cfg, rules, clock):
    cfg.activity.reminders_enabled = False
    h = Harness(cfg, rules, clock)
    h.run(90 * 60, step=10)
    assert h.alerts("reminder") == [] and not h.mon.reminders.state.active
    assert h.activity[-1].remaining_seconds is None


def test_restriction_and_activity_are_independent(h):
    h.run(10)
    h.ocr.default = POPUP_TEXT
    h.run(60, step=10)
    assert len(h.alerts("incident")) == 1 and len(h.alerts("activity")) == 1
    h.close_studio()
    h.run(30)
    assert len(h.alerts("incident")) == 1 and len(h.alerts("activity")) == 2
    hist = h.queue.history(kind="activity")
    assert all(x["kind"] != "incident" for x in hist) and sum(x["kind"] == "activity" for x in hist) == 2
    assert len(h.queue.history(kind="incident")) == 1 and len(h.queue.history(kind="all")) == len(hist) + 1
    assert all("Delivered to" in x["detail"] for x in h.queue.history(kind="all"))


def test_privacy_masks_apply_to_all_activity_screenshots(cfg, rules, clock):
    cfg.regions = [Region("chat", 0.5, 0.0, 0.5, 1.0, "redact")]
    cfg.activity.close_debounce_seconds = 4
    h = Harness(cfg, rules, clock)
    h.run(61 * 60, step=10)
    h.close_studio()
    h.run(20)
    paths = [a["screenshot_path"] for a in h.alerts("activity") + h.alerts("reminder")]
    paths.append(str(cfg.frame_cache_dir / "latest.png"))
    assert len(paths) == 4 and all(paths)
    for p in paths:
        img = Image.open(p)
        assert img.getpixel((img.width - 1, 0)) == (0, 0, 0) and img.getpixel((0, 0)) == (40, 40, 40)


def test_text_only_when_screenshots_disabled(cfg, rules, clock):
    cfg.privacy.send_screenshots = False
    h = Harness(cfg, rules, clock)
    h.run(61 * 60, step=10)
    assert all(a["screenshot_path"] == "" for a in h.alerts("activity") + h.alerts("reminder"))
    assert not any(cfg.activity_screenshots_dir.glob("*.png"))


def test_activity_snapshot_fields(h):
    h.run(5 * 60, step=10)
    s = h.activity[-1]
    assert s.app_state == "RUNNING" and s.live_state == "NOT_LIVE" and s.live_rules_verified is False
    assert "go_live_control" in s.live_evidence and s.last_confirmed_utc.endswith("+00:00")
    assert s.episode_id.startswith("EP-") and s.remaining_seconds is not None
    assert s.last_event.startswith(EVT_ALREADY_RUNNING) and "counts" in s.delivery


# ---------------------------------------------------------------- settings

def test_settings_persist_and_migrate(tmp_path):
    old = {"machine_label": "pc", "telegram": {"bot_token": "t"}}   # version-1 file: no activity section
    c = AppConfig.from_dict(old)
    a = c.activity
    assert (a.notify_opened, a.notify_closed, a.notify_already_running, a.reminders_enabled) == (True, True, True, True)
    assert a.offline_threshold_minutes == 60 and a.repeat_enabled is False and a.start_at_signin is False
    c.activity.offline_threshold_minutes = 45
    c.activity.repeat_enabled = True
    c.regions = [Region("live badge", 0.1, 0.1, 0.2, 0.1, "live")]
    p = tmp_path / "c.json"
    c.save(p)
    back = AppConfig.load(p)
    assert back.activity.offline_threshold_minutes == 45 and back.activity.repeat_enabled
    assert back.live_regions == c.regions and back.config_version == 4
    assert AppConfig.from_dict({"activity": {"bogus": 1, "notify_opened": False}}).activity.notify_opened is False
