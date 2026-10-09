from conftest import FakeWindowSystem, make_window

from studio_monitor.config import TargetIdentity
from studio_monitor.target import identity_from_window, rediscover, related_windows, validate_handle
from studio_monitor.tracker import Status, WindowTracker
from studio_monitor.win32.windows import Rect, looks_like_studio, selectable_windows


def test_identity_discovers_exe_from_window():
    ident = identity_from_window(make_window())
    assert ident.exe_name == "TikTok LIVE Studio.exe" and ident.class_name == "Chrome_WidgetWin_1"


def test_validate_rejects_stale_handle_reused_by_other_process(studio_identity):
    sys_ = FakeWindowSystem()
    sys_.add(make_window(hwnd=0x1001, title="Notepad", exe="C:/Windows/notepad.exe", pid=9, cls="Notepad"))
    r = validate_handle(sys_, studio_identity)
    assert not r.ok and "notepad.exe" in r.reason


def test_validate_rejects_dead_process(studio_identity):
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    sys_.alive.clear()
    assert "process" in validate_handle(sys_, studio_identity).reason


def test_rediscover_after_update_changes_install_path(studio_identity):
    sys_ = FakeWindowSystem()
    sys_.add(make_window(hwnd=0x2002, title="TikTok LIVE Studio", pid=5151,
                         exe="C:/Program Files/TikTok LIVE Studio/1.37.0/TikTok LIVE Studio.exe"))
    sys_.add(make_window(hwnd=0x2003, title="TikTok LIVE Studio", pid=5151, rect=Rect(0, 0, 300, 200),
                         exe="C:/Program Files/TikTok LIVE Studio/1.37.0/TikTok LIVE Studio.exe"))  # splash
    sys_.add(make_window(hwnd=0x3003, title="TikTok LIVE Studio", pid=77, exe="C:/evil/fake.exe"))
    found = rediscover(sys_, studio_identity)
    assert found is not None and found.hwnd == 0x2002


def test_rediscover_requires_class_match(studio_identity):
    sys_ = FakeWindowSystem()
    sys_.add(make_window(hwnd=0x2002, cls="OtherClass"))
    assert rediscover(sys_, studio_identity) is None


def test_related_windows_includes_dialogs_from_process_tree_and_owned():
    sys_ = FakeWindowSystem()
    main = sys_.add(make_window())
    sys_.add(make_window(hwnd=0x5001, title="Notice", pid=4242, rect=Rect(300, 300, 800, 600)))        # same pid
    sys_.add(make_window(hwnd=0x5002, title="Helper", pid=4300, rect=Rect(300, 300, 800, 600)))        # child proc
    sys_.add(make_window(hwnd=0x5003, title="Owned", pid=999, owner=0x1001, rect=Rect(0, 0, 500, 400)))  # owned
    sys_.add(make_window(hwnd=0x5004, title="Other", pid=1, exe="C:/x.exe"))
    sys_.add(make_window(hwnd=0x5005, title="Hidden", pid=4242, visible=False))
    sys_.children[4242] = {4300}
    found = {w.hwnd for w in related_windows(sys_, main)}
    assert found == {0x5001, 0x5002, 0x5003}


def test_tracker_lifecycle(studio_identity, clock):
    sys_ = FakeWindowSystem()
    main = sys_.add(make_window())
    saved = []
    t = WindowTracker(sys_, studio_identity, clock, on_identity_change=saved.append)
    assert t.poll().status == Status.RUNNING

    # move/resize is followed
    sys_.windows[0x1001] = make_window(rect=Rect(200, 200, 1000, 700))
    t.poll()
    assert t.state.last_rect == Rect(200, 200, 1000, 700)
    assert any("moved/resized" in e for e in t.drain_events())

    # minimized -> DEGRADED
    sys_.windows[0x1001] = make_window(minimized=True)
    assert t.poll().status == Status.DEGRADED and "minimized" in t.state.reason

    # closed -> LOST
    sys_.remove(0x1001)
    sys_.alive.clear()
    assert t.poll().status == Status.LOST
    assert t.poll().status == Status.LOST

    # restart with new pid/hwnd -> rediscovered, identity updated and persisted
    sys_.add(make_window(hwnd=0x9009, pid=8080))
    assert t.poll().status == Status.RUNNING
    assert t.identity.hwnd == 0x9009 and t.identity.pid == 8080
    assert saved and saved[-1].hwnd == 0x9009
    assert t.state.rediscovered_count == 1


def test_tracker_degraded_on_capture_failure_and_offscreen(studio_identity, clock):
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    t = WindowTracker(sys_, studio_identity, clock)
    t.poll()
    t.report_capture(False, False)
    assert t.state.status == Status.DEGRADED
    t.poll()
    t.report_capture(True, False, "screen-grab fallback")
    assert t.state.status == Status.DEGRADED and "fallback" in t.state.reason
    sys_.windows[0x1001] = make_window(rect=Rect(-32000, -32000, -31000, -31500))
    assert t.poll().status == Status.DEGRADED and "off-screen" in t.state.reason


def test_selectable_windows_filters_and_heuristic():
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    sys_.add(make_window(hwnd=2, title="", pid=3))
    sys_.add(make_window(hwnd=3, title="tool", pid=4, tool=True))
    sys_.add(make_window(hwnd=4, title="tiny", pid=5, rect=Rect(0, 0, 10, 10)))
    sys_.add(make_window(hwnd=5, title="Notepad", pid=6, exe="C:/notepad.exe"))
    rows = selectable_windows(sys_, own_pid=0)
    assert [w.hwnd for w in rows] == [5, 0x1001]
    assert looks_like_studio(rows[1]) and not looks_like_studio(rows[0])


def test_identity_is_set():
    assert not TargetIdentity().is_set and identity_from_window(make_window()).is_set
