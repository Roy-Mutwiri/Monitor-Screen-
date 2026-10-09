"""GUI tests: build the real window (withdrawn), drive every button, switch
pages, save settings and the owner name, exercise bots/history flows.
Dialogs and the monitor builder are replaced with test doubles; nothing
reaches Telegram or the real credential store."""
import threading
from pathlib import Path

import pytest

from conftest import TOKEN_A, TOKEN_B, FakeClock

import tkinter as tk
import ttkbootstrap as tb

from studio_monitor.bots import BotRegistry, CAT_RESTRICTIONS, BotTarget
from studio_monitor.config import AppConfig
from studio_monitor.credentials import MemoryCredentialStore
from studio_monitor.gui import app as gui
from studio_monitor.queue import DeliveryQueue
from studio_monitor.monitor import ActivitySnapshot
from studio_monitor.health import HealthSnapshot
from studio_monitor.win32.capture import CaptureStatus

pytestmark = pytest.mark.skipif(not hasattr(tk, "Tk"), reason="tk unavailable")


class FakeMonitor:
    def __init__(self, queue):
        self.queue = queue
        self.worker = None
        self.client_factory = None
        self.stopped = False
        self.frames = type("F", (), {"status": staticmethod(lambda: CaptureStatus())})()

    def start_background(self):
        t = threading.Thread(target=lambda: None)
        t.start()
        return t

    def stop(self):
        self.stopped = True


class Answers:
    """Scripted stand-ins for tkinter dialogs."""
    def __init__(self):
        self.calls = []

    def askyesno(self, title, msg):
        self.calls.append(("askyesno", title)); return True

    def showinfo(self, title, msg):
        self.calls.append(("showinfo", title))

    def showerror(self, title, msg):
        self.calls.append(("showerror", title, msg))

    def askopenfilename(self, **kw):
        self.calls.append(("askopenfilename", kw.get("title"))); return ""


@pytest.fixture
def ui(tmp_path, monkeypatch):
    answers = Answers()
    for name in ("askyesno", "showinfo", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, getattr(answers, name))
    monkeypatch.setattr(gui.filedialog, "askopenfilename", answers.askopenfilename)
    store = MemoryCredentialStore()
    monkeypatch.setattr(gui, "make_registry", lambda cfg, path, queue=None, store_=None:
                        BotRegistry(cfg, store, save=lambda: cfg.save(path), queue=queue))
    monitors = []

    def fake_build_monitor(cfg, cfg_path, registry=None, queue=None, **cb):
        m = FakeMonitor(queue); monitors.append(m); return m
    monkeypatch.setattr(gui, "build_monitor", fake_build_monitor)
    monkeypatch.setattr(gui, "setup_logging", lambda cfg: None)

    class FakeBotDialog:
        opened = []

        def __init__(self, parent, registry, factory, bg, bot=None):
            FakeBotDialog.opened.append(bot)
            self.result = None
    monkeypatch.setattr(gui, "BotDialog", FakeBotDialog)

    class FakeEnrollDialog:
        opened = []

        def __init__(self, parent, url="", mode="standalone"):
            FakeEnrollDialog.opened.append((url, mode))
            self.result = None
    monkeypatch.setattr(gui, "EnrollDialog", FakeEnrollDialog)
    monkeypatch.setattr(gui, "validate_bot", lambda factory, reg, bot_id: {"id": 7, "username": "fake_bot", "first_name": "F"})
    monkeypatch.setattr(gui, "enqueue_test", lambda q, r, c, bot_id: "TEST-1")
    monkeypatch.setattr(gui, "deliver_test_now", lambda q, r, f, eid: "test delivered (fake)")
    monkeypatch.setattr(gui.App, "_open_path", lambda self, path: answers.calls.append(("open", path)))
    monkeypatch.setattr(gui.App, "_deliver_pending_once", lambda self: answers.calls.append(("deliver_pending",)))

    cfg_path = tmp_path / "config.json"
    cfg = AppConfig(); cfg.data_dir = str(tmp_path / "data"); cfg.machine_label = "test-pc"
    cfg.telegram.fingerprint_salt = "s"
    cfg.save(cfg_path)
    root = None
    for attempt in range(3):   # Tcl occasionally fails to source its library when interpreters are created back to back
        try:
            root = tb.Window(theme="bootstrap-dark")
            break
        except tk.TclError:
            if attempt == 2:
                raise
            import time as _t
            _t.sleep(0.3)
    root.withdraw()
    app = gui.App(root, cfg, cfg_path)
    root.update()
    yield app, answers, store, monitors, FakeBotDialog
    try:
        app.on_close()
    except tk.TclError:
        pass


def pump(app, n=3):
    for _ in range(n):
        app.root.update()


def wait_until(app, predicate, timeout=4.0):
    import time as _t
    end = _t.time() + timeout
    while _t.time() < end:
        app.root.update()
        if predicate():
            return True
        _t.sleep(0.05)
    return predicate()


def all_buttons(widget):
    out = []
    for child in widget.winfo_children():
        if isinstance(child, tb.Button) or child.winfo_class() in ("TButton",):
            out.append(child)
        out.extend(all_buttons(child))
    return out


def test_every_button_invokes_without_error(ui):
    app, answers, store, monitors, dialog = ui
    reg = app.registry
    reg.add("A", TOKEN_A, "1")
    app.refresh_bots(); pump(app)
    app.bots_tree.selection_set(reg.bots[0].bot_id); app._update_bot_buttons()
    app.cfg.target.hwnd = 0   # start() without a target shows an info dialog instead of starting
    seen = 0
    for key, *_ in gui.PAGES:
        app.show_page(key); pump(app)
        for btn in all_buttons(app.pages[key]) + all_buttons(app.root.winfo_children()[0]):
            if str(btn.cget("state")) == "disabled":
                continue
            btn.invoke(); pump(app); seen += 1
    assert seen >= 20
    assert ("showinfo", "Start") in answers.calls                      # Start without target -> guided, no crash
    assert dialog.opened == [None, reg.bots[0]] or None in dialog.opened  # Add and Edit opened the dialog
    assert any(c[0] == "askopenfilename" for c in answers.calls)       # calibration buttons asked for a file


def test_navigation_switches_pages(ui):
    app, *_ = ui
    for key, *_ in gui.PAGES:
        app.nav_buttons[key].invoke(); pump(app)
        assert app.nav_var.get() == key
        assert app.pages[key].winfo_manager() == "pack"
        assert all(app.pages[k].winfo_manager() == "" for k in app.pages if k != key)


def test_owner_save_validation_and_persistence(ui):
    app, answers, *_ = ui
    app.owner_var.set("  Roy  ")
    app.owner_save_btn.invoke(); pump(app)
    assert app.cfg.owner_name == "Roy" and "Roy’s Live" in app.owner_label_var.get()
    assert AppConfig.load(app.cfg_path).owner_name == "Roy"
    app.owner_var.set("bad\nname")
    app.owner_save_btn.invoke()
    assert any(c[0] == "showerror" and c[1] == "Whose PC?" for c in answers.calls) and app.cfg.owner_name == "Roy"


def test_settings_save_validates_and_persists_and_switches_theme(ui):
    app, answers, *_ = ui
    app.show_page("settings"); pump(app)
    app.set_vars["threshold"].set("45")
    app.set_vars["send_shots"].set(False)
    app.set_vars["ui_theme"].set("Light")
    app.settings_save_btn.invoke(); pump(app)
    assert app.cfg.activity.offline_threshold_minutes == 45 and app.cfg.privacy.send_screenshots is False
    assert app.cfg.ui.theme == "bootstrap-light" and app.style.theme_use() == "bootstrap-light"
    back = AppConfig.load(app.cfg_path)
    assert back.activity.offline_threshold_minutes == 45 and back.ui.theme == "bootstrap-light"
    app.set_vars["poll"].set("abc")
    app.settings_save_btn.invoke()
    assert any(c[0] == "showerror" and "Poll interval" in c[2] for c in answers.calls)
    assert app.cfg.detection.poll_interval_seconds == 1.0
    app.set_vars["poll"].set("9")
    app.settings_revert_btn.invoke()
    assert app.set_vars["poll"].get() == "1.0"


def test_start_and_stop_buttons(ui):
    app, answers, store, monitors, _ = ui
    app.registry.add("A", TOKEN_A, "1")
    app.cfg.target.hwnd = 0x1001; app.cfg.target.exe_name = "x.exe"; app.cfg.target.class_name = "C"
    app.start_btn.invoke(); pump(app)
    assert monitors and app.monitor is monitors[0]
    assert str(app.start_btn.cget("state")) == "disabled" and str(app.stop_btn.cget("state")) == "normal"
    app.stop_btn.invoke(); pump(app)
    assert monitors[0].stopped and app.monitor is None and str(app.start_btn.cget("state")) == "normal"


def test_bots_page_buttons_toggle_validate_test_remove(ui):
    app, answers, store, *_ = ui
    bot = app.registry.add("A", TOKEN_A, "1")
    app.show_page("bots"); pump(app)
    assert "1 / 10" in app.bots_count_pill.cget("text")
    app.bots_tree.selection_set(bot.bot_id); app._update_bot_buttons()
    app.bot_btns["Enable/Disable"].invoke(); pump(app)
    assert bot.enabled is False and "disabled" in app.bots_tree.item(bot.bot_id, "values")[3]
    app.bots_tree.selection_set(bot.bot_id); app._update_bot_buttons()
    app.bot_btns["Validate Bot"].invoke()
    assert wait_until(app, lambda: "fake_bot" in app.bots_status.get())
    app.bots_tree.selection_set(bot.bot_id); app._update_bot_buttons()
    app.bot_btns["Send Test"].invoke()
    assert wait_until(app, lambda: "test delivered" in app.bots_status.get())
    app.bots_tree.selection_set(bot.bot_id); app._update_bot_buttons()
    app.bot_btns["Remove"].invoke(); pump(app)
    assert app.registry.count == 0 and bot.bot_id not in store.tokens


def test_history_filter_details_and_retry(ui):
    app, answers, *_ = ui
    q = app.queue
    q.create_event("E1", "incident", CAT_RESTRICTIONS, {"text": "x"}, "", [BotTarget("b", "B", "1", None)], label="Restriction",
                   owner_label="Roy’s Live")
    d = q.deliveries_for("E1")[0]
    q.mark_failed(d.id, "token rejected", permanent=True)
    app.show_page("history"); pump(app)
    rows = app.history.get_children()
    assert len(rows) == 1 and app.history.item(rows[0], "values")[3] == "Roy’s Live"
    app.history.selection_set(rows[0]); app._show_event_details(); pump(app)
    drows = app.details.get_children()
    assert len(drows) == 1 and app.details.item(drows[0], "values")[2] == "failed"
    app.details.selection_set(drows[0]); app._update_retry_button()
    assert str(app.retry_btn.cget("state")) == "normal"
    app.retry_btn.invoke(); pump(app)
    assert q.delivery(d.id).status == "pending" and ("deliver_pending",) in answers.calls
    app._history_kind.set("activity"); app.refresh_history()
    assert app.history.get_children() == ()


def test_activity_snapshot_renders_tiles_and_pills(ui):
    app, *_ = ui
    snap = ActivitySnapshot(app_state="RUNNING", session_id="SES-1", live_state="LIVE", live_evidence="LIVE: end_live_control",
                            last_confirmed_utc="2026-10-09T10:00:00+00:00", offline_seconds=0, remaining_seconds=None,
                            episode_id="", reminders_sent=0, last_event="BROADCAST_STARTED",
                            delivery={"counts": {"pending": 0, "sent": 3, "failed": 0, "dead": 0, "cancelled": 0},
                                      "last": {"id": "BCS-1", "status": "sent", "bot": "A", "error": ""}},
                            health=HealthSnapshot(capture="OK", capture_backend="wgc"),
                            capture=CaptureStatus(health="OK", backend="wgc", frames=12, last_valid_at=1_800_000_000.0))
    app._show_activity(snap); pump(app)
    assert app.tile_live.value.cget("text") == "LIVE" and "Broadcast: live" in app.pill_live.cget("text")
    assert app.tile_capture.value.cget("text") == "OK" and "Windows Graphics Capture" in app.tile_capture.caption.cget("text")
    assert "3 sent" in app.tile_delivery.value.cget("text") and "Capture: OK" in app.pill_capture.cget("text")


def test_region_tools_and_clear(ui):
    app, answers, *_ = ui
    from PIL import Image
    from studio_monitor.regions import Region
    app.cfg.regions = [Region("detect 1", 0.1, 0.1, 0.3, 0.3)]
    app._refresh_region_list()
    app.preview_image = Image.new("RGB", (640, 360), (20, 20, 20))
    app._draw_preview()
    app.region_list.selection_set(0)
    app.region_remove_btn.invoke()
    assert app.cfg.regions == []
    app.cfg.regions.append(Region("live 1", 0.5, 0.5, 0.2, 0.2, "live")); app._refresh_region_list()
    app.region_clear_btn.invoke()
    assert app.cfg.regions == [] and ("askyesno", "Clear regions") in answers.calls


def test_diagnostics_text_and_copy(ui):
    app, answers, *_ = ui
    app.show_page("diagnostics"); pump(app)
    text = app.diag.text.get("1.0", "end")
    assert "hostname=" in text and "[capture]" in text and "[outbox]" in text
    app.diag_copy_btn.invoke()
    assert "hostname=" in app.root.clipboard_get()
    app.open_data_btn.invoke()
    assert any(c[0] == "open" for c in answers.calls)
