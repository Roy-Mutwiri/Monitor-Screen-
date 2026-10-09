"""Automatic @username discovery: extraction rules, guarded interaction,
episode control, state-engine pause, notifications. Fully mocked."""
from pathlib import Path

import pytest
from PIL import Image

from conftest import TOKEN_A, TOKEN_B, FakeWindowSystem, all_deliveries, fake_frame, make_window
from test_activity import LIVE_TEXT, LOADING_TEXT, NOT_LIVE_TEXT, Harness

from studio_monitor import account as acct
from studio_monitor.account import (FAILED, SUCCEEDED, AccountIdentity, IdentityStore, LookupContext, click_point,
                                    extract_username, perform_lookup)
from studio_monitor.bots import CAT_BROADCAST
from studio_monitor.broadcast import LiveState
from studio_monitor.config import TargetIdentity
from studio_monitor.regions import Region
from studio_monitor.win32.windows import Rect

MENU_TEXT = "Roy Mutwiri\n@roy.mutwiri_1\nMy profile\nSwitch account\nLog out"
MENU_SIZE = (320, 240)   # popup window size -> FakeOcr key


# ---------------------------------------------------------------- extraction

@pytest.mark.parametrize("texts,user,display", [
    ([MENU_TEXT], "roy.mutwiri_1", "Roy Mutwiri"),
    (["@User_Name.99", "@User_Name.99"], "User_Name.99", None),
    (["Display Name Only\nSwitch account\nLog out"], None, "Display Name Only"),   # no handle -> unavailable
    (["@one.handle", "@other_handle"], None, None),                                # frames disagree -> ambiguous
    (["Roy @roy @roy2"], None, None),                                              # several handles -> ambiguous
    (["", ""], None, None),
    (["email me at test@mail.com"], None, None),                                   # not a handle (preceded by text)
])
def test_extract_username(texts, user, display):
    u, d, why = extract_username(texts)
    assert u == user
    if display is not None:
        assert d == display
    if user is None:
        assert why


def test_click_point_calibrated_vs_default():
    win = make_window(rect=Rect(100, 50, 1300, 850))
    assert click_point(win, None, 190, 24) == (1110, 74)
    region = Region("profile", 0.9, 0.0, 0.05, 0.06, "profile")
    x, y = click_point(win, region, 190, 24)
    assert 100 + 0.9 * 1200 <= x <= 100 + 0.95 * 1200 and 50 <= y <= 50 + 0.06 * 800


# ---------------------------------------------------------------- fake interactor

class FakeInteractor:
    """Scripted OS interaction. ``menu_texts`` -> what the popup OCR yields."""

    def __init__(self, sys_, menu_text=MENU_TEXT, fg=0x1001, idle=10.0, hit_ok=True, popup=True,
                 uia=None, bring_ok=True):
        self.sys = sys_
        self.menu_text = menu_text
        self.fg = fg
        self.idle = idle
        self.hit_ok = hit_ok
        self.popup = popup
        self.uia = uia
        self.bring_ok = bring_ok
        self.clicks: list[tuple[int, int]] = []
        self.escapes = 0
        self.brought: list[int] = []
        self.menu_open = False

    def foreground(self):
        return self.fg

    def bring_to_front(self, hwnd):
        self.brought.append(hwnd)
        if self.bring_ok:
            self.fg = hwnd
        return self.bring_ok

    def idle_seconds(self):
        return self.idle

    def hit_test(self, x, y):
        return 0x1001 if self.hit_ok else 0x7777

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.popup:
            self.menu_open = not self.menu_open

    def escape(self):
        self.escapes += 1
        self.menu_open = False

    def studio_windows(self, main):
        wins = {main.hwnd: main}
        if self.menu_open:
            wins[0x9A9A] = make_window(hwnd=0x9A9A, title="", pid=main.pid, rect=Rect(900, 40, 1220, 280))
        return wins

    def capture_window(self, win):
        return fake_frame(*win.rect.size)

    def uia_read(self, hwnd):
        return self.uia


def make_ctx(sys_, inter, ocr_map=None, **kw):
    ocr_map = ocr_map or {MENU_SIZE: inter.menu_text}

    def ocr(img):
        return ocr_map.get(img.size, "")
    ident = TargetIdentity(hwnd=0x1001, pid=4242, exe_path="C:/x/TikTok LIVE Studio.exe", exe_name="TikTok LIVE Studio.exe",
                           class_name="Chrome_WidgetWin_1", title="TikTok LIVE Studio")
    t = [0.0]

    def mono():
        return t[0]

    def sleep(s):
        t[0] += s
    defaults = dict(system=sys_, interactor=inter, identity=ident, ocr=ocr, fresh_frame=lambda: fake_frame(1280, 720),
                    timeout=10.0, idle_required=1.5, clock=lambda: 1_700_000_000.0, mono=mono, sleep=sleep)
    defaults.update(kw)
    return LookupContext(**defaults)


def test_lookup_success_via_popup_and_cleanup():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_)
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == SUCCEEDED and res.username == "roy.mutwiri_1" and res.display_name == "Roy Mutwiri"
    assert res.source == "popup-ocr" and len(inter.clicks) == 1 and inter.escapes == 1
    assert res.closed_menu and not inter.menu_open and res.attempts == 1
    assert inter.brought == []            # Studio was already in front: focus untouched


def test_lookup_prefers_accessibility_without_clicking():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, uia=["TikTok LIVE Studio", "Roy Mutwiri", "@roy.mutwiri_1", "Log out"])
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == SUCCEEDED and res.source == "uia" and inter.clicks == []


def test_display_name_only_is_unavailable_not_fabricated():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, menu_text="Roy Mutwiri\nSwitch account\nLog out")
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == FAILED and res.username == "" and "no @handle" in res.error
    assert res.closed_menu and inter.escapes == 1


def test_ambiguous_frames_rejected():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_)
    texts = iter(["@roy.one\nLog out", "@roy.0ne\nLog out"])
    ctx = make_ctx(sys_, inter, ocr_map={})
    ctx.ocr = lambda img: next(texts, "")
    res = perform_lookup(ctx)
    assert res.status == FAILED and "ambiguous" in res.error and res.username == ""


def test_wrong_foreground_brings_forward_once_and_restores():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, fg=0x5555)
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == SUCCEEDED and inter.brought[0] == 0x1001 and res.focus_changed
    assert inter.brought[-1] == 0x5555 and res.focus_restored          # previous window restored


def test_foreground_acquisition_failure_fails_safely():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, fg=0x5555, bring_ok=False)
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == FAILED and inter.clicks == [] and "foreground" in res.error
    assert len(inter.brought) == 1                                     # never fights for focus repeatedly


def test_occluded_target_prevents_click():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, hit_ok=False)
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == FAILED and inter.clicks == [] and "covered" in res.error


def test_user_activity_and_blocking_dialog_defer_then_fail():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, idle=0.2)
    res = perform_lookup(make_ctx(sys_, inter))
    assert res.status == FAILED and inter.clicks == [] and "deferred" in res.error
    inter2 = FakeInteractor(sys_)
    res2 = perform_lookup(make_ctx(sys_, inter2, blocked=lambda: True))
    assert res2.status == FAILED and inter2.clicks == []


def test_target_exit_during_lookup():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, idle=0.0)
    ctx = make_ctx(sys_, inter)
    calls = {"n": 0}

    def idle():
        calls["n"] += 1
        if calls["n"] == 3:
            sys_.remove(0x1001); sys_.alive.clear()
        return 0.0 if calls["n"] < 3 else 10.0
    inter.idle_seconds = idle
    res = perform_lookup(ctx)
    assert res.status == FAILED and "exited" in res.error and inter.clicks == []


def test_no_popup_gets_one_bounded_retry_then_frame_ocr():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_, popup=False)
    res = perform_lookup(make_ctx(sys_, inter, ocr_map={}))
    assert res.status == FAILED and len(inter.clicks) == 2 and "retry" in " ".join(res.steps)
    inter2 = FakeInteractor(sys_, popup=False)
    res2 = perform_lookup(make_ctx(sys_, inter2, ocr_map={(576, 432): "Roy\n@roy_frame"}))   # top-right crop of 1280x720
    assert res2.status == SUCCEEDED and res2.source == "frame-ocr"


def test_physical_interaction_can_be_disabled():
    sys_ = FakeWindowSystem(); sys_.add(make_window())
    inter = FakeInteractor(sys_)
    res = perform_lookup(make_ctx(sys_, inter, allow_physical=False))
    assert res.status == FAILED and inter.clicks == [] and "disabled" in res.error


# ---------------------------------------------------------------- monitor integration

def live_harness(cfg, rules, clock, **kw):
    h = Harness(cfg, rules, clock)
    h.inter = FakeInteractor(h.sys, **kw)
    h.mon.interactor = h.inter
    h.mon._inline_lookup = True
    h.mon._lookup_sleep = lambda s: None
    h.ocr.texts[MENU_SIZE] = h.inter.menu_text
    return h


def broadcast_alerts(h):
    return [d for d in all_deliveries(h.queue) if "GONE LIVE" in d["payload"]["caption"] or "ALREADY LIVE" in d["payload"]["caption"]]


def test_one_lookup_per_broadcast_with_username_and_preserved_screenshot(cfg, rules, clock):
    cfg.owner_name = "Roy"
    h = live_harness(cfg, rules, clock)
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = broadcast_alerts(h)
    assert len(ev) == 1
    cap = ev[0]["payload"]["caption"]
    assert "Roy\u2019s Live \u2014 HAS GONE LIVE" in cap and "TikTok account: @roy.mutwiri_1" in cap and "Detected at:" in cap
    assert Path(ev[0]["screenshot_path"]).exists()
    img = Image.open(ev[0]["screenshot_path"])
    assert img.size == (1280, 720)                                     # the broadcast frame, not the 320x240 menu
    assert len(h.inter.clicks) == 1 and h.inter.escapes == 1
    assert h.mon.account.status == SUCCEEDED and h.mon.current_account_handle() == "@roy.mutwiri_1"
    h.run(10 * 60, step=10)
    assert len(h.inter.clicks) == 1 and len(broadcast_alerts(h)) == 1  # repeated LIVE polls: no more lookups/alerts


def test_already_live_attachment_looks_up_once_with_already_live_wording(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    ev = broadcast_alerts(h)
    assert len(ev) == 1 and "ALREADY LIVE" in ev[0]["payload"]["caption"]
    assert "TikTok account: @roy.mutwiri_1" in ev[0]["payload"]["caption"]
    assert len(h.inter.clicks) == 1


def test_no_lookup_on_studio_open_not_live_or_unknown(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.run(5 * 60, step=10)                       # NOT_LIVE
    h.ocr.default = LOADING_TEXT
    h.run(2 * 60, step=10)                       # UNKNOWN
    assert h.inter.clicks == [] and broadcast_alerts(h) == []


def test_unknown_recovery_and_restart_do_not_repeat_lookup(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    assert len(h.inter.clicks) == 1
    h.ocr.default = LOADING_TEXT
    h.run(3 * 60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    assert len(h.inter.clicks) == 1 and len(broadcast_alerts(h)) == 1
    # monitor restart while still live: identity persisted, no second menu interaction
    h2 = Harness(cfg, rules, clock, sys_=h.sys, queue=h.queue)
    inter2 = FakeInteractor(h2.sys); h2.mon.interactor = inter2; h2.mon._inline_lookup = True; h2.mon._lookup_sleep = lambda s: None
    h2.ocr.default = LIVE_TEXT
    h2.run(60, step=10)
    ev = broadcast_alerts(h2)
    assert inter2.clicks == [] and len(ev) == 2 and "ALREADY LIVE" in ev[1]["payload"]["caption"]
    assert "TikTok account: @roy.mutwiri_1" in ev[1]["payload"]["caption"]   # reused only for the same episode


def test_new_broadcast_reads_account_again_and_clears_previous(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(60, step=10)
    assert h.mon.account.username == "roy.mutwiri_1"
    h.ocr.default = NOT_LIVE_TEXT; h.run(60, step=10)
    assert h.mon.current_account_handle() == "" and h.mon.account_snapshot()["is_current"] is False   # "last detected"
    h.inter.menu_text = "Other Person\n@second.account\nLog out"
    h.ocr.texts[MENU_SIZE] = h.inter.menu_text
    h.ocr.default = LIVE_TEXT; h.run(60, step=10)
    assert len(h.inter.clicks) == 2 and h.mon.account.username == "second.account"
    caps = [d["payload"]["caption"] for d in broadcast_alerts(h)]
    assert "@roy.mutwiri_1" in caps[0] and "@second.account" in caps[1]


def test_lookup_failure_still_sends_alert_with_unavailable_wording(cfg, rules, clock):
    h = live_harness(cfg, rules, clock, menu_text="Roy Mutwiri\nLog out")
    h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(60, step=10)
    ev = broadcast_alerts(h)
    assert len(ev) == 1
    cap = ev[0]["payload"]["caption"]
    assert "TikTok account: unavailable \u2014 automatic lookup failed" in cap and "HAS GONE LIVE" in cap
    assert h.mon.account.status == FAILED and Path(ev[0]["screenshot_path"]).exists()


def test_timeout_still_sends_alert(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.mon.interactor = FakeInteractor(h.sys)
    h.mon._lookup_sleep = lambda s: None
    # job that never finishes: replace the start method so the thread is not started
    acct.AccountLookupJob.start = lambda self: None
    try:
        h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(6, step=2)
        assert h.mon._lookup is not None and broadcast_alerts(h) == []    # waiting within the 10 s budget
        h.run(14, step=2)
        ev = broadcast_alerts(h)
        assert len(ev) == 1 and "automatic lookup failed" in ev[0]["payload"]["caption"]
        assert "timed out" in ev[0]["payload"]["text"] and h.mon._lookup is None
    finally:
        del acct.AccountLookupJob.start


def test_profile_menu_does_not_cause_false_offline_or_second_episode(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    inter = FakeInteractor(h.sys)
    h.mon.interactor = inter
    h.mon._lookup_sleep = lambda s: None
    acct.AccountLookupJob.start = lambda self: None     # keep the lookup "open" for a while
    try:
        h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(6, step=2)
        episode = h.mon.episodes.state.episode_id
        assert episode and h.mon._lookup is not None
        h.ocr.default = NOT_LIVE_TEXT                    # menu hides live indicators -> looks NOT_LIVE
        h.run(6, step=2)
        assert h.mon.broadcast.paused and h.mon.broadcast.confirmed == LiveState.LIVE
        assert not h.mon.reminders.state.active
        h.ocr.default = LIVE_TEXT
        h.run(20, step=2)                                # lookup times out, engine resumes
        assert not h.mon.broadcast.paused and h.mon.episodes.state.episode_id == episode
        h.run(60, step=10)
        assert len(broadcast_alerts(h)) == 1
    finally:
        del acct.AccountLookupJob.start


def test_restriction_alert_continues_during_lookup_and_carries_account(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(60, step=10)
    h.ocr.default = "Your LIVE has been restricted for violating our Community Guidelines"
    h.run(10, step=10)
    inc = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"]
    assert len(inc) == 1 and "TikTok account: @roy.mutwiri_1" in inc[0]["payload"]["caption"]


def test_detection_disabled_sends_alert_without_interaction(cfg, rules, clock):
    cfg.account.detect_on_broadcast = False
    h = live_harness(cfg, rules, clock)
    h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(60, step=10)
    ev = broadcast_alerts(h)
    assert len(ev) == 1 and "not detected (automatic detection disabled)" in ev[0]["payload"]["caption"]
    assert h.inter.clicks == []


def test_owner_label_and_username_to_multiple_bots(cfg, rules, clock, tmp_path):
    cfg.owner_name = "Roy"
    h = live_harness(cfg, rules, clock)
    h.registry.add("Second", TOKEN_B, "2", subscriptions=[CAT_BROADCAST])
    h.run(60, step=10); h.ocr.default = LIVE_TEXT; h.run(60, step=10)
    ev = broadcast_alerts(h)
    assert sorted(d["bot_name"] for d in ev) == ["Default Bot", "Second"]
    for d in ev:
        assert "Roy\u2019s Live \u2014 HAS GONE LIVE" in d["payload"]["caption"] and "@roy.mutwiri_1" in d["payload"]["caption"]
    assert len({d["screenshot_path"] for d in ev}) == 1


def test_identity_store_roundtrip(tmp_path, clock):
    from studio_monitor.queue import DeliveryQueue
    q = DeliveryQueue(tmp_path / "q.sqlite3", clock=clock)
    store = IdentityStore(q)
    assert store.load().status == "NOT_ATTEMPTED"
    store.save(AccountIdentity(episode_id="BC-1", status=SUCCEEDED, username="roy", source="popup-ocr", observed_utc="t"))
    back = store.load()
    assert back.username == "roy" and back.handle == "@roy" and back.account_line() == "@roy"
    assert AccountIdentity(status=FAILED).account_line() == "unavailable \u2014 automatic lookup failed"
