"""Tkinter GUI: pick the Studio window, preview it, draw regions, monitor,
and show Studio activity (session, broadcast state, reminder progress)."""
from __future__ import annotations

import queue as _queue
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

from PIL import Image, ImageTk

from .. import SOURCE_LABEL, __version__
from ..alerts import format_duration
from ..app import build_monitor, load_live_rules, load_ruleset, setup_logging
from ..config import AppConfig
from ..monitor import ActivitySnapshot, Monitor, StatusUpdate
from ..regions import Region
from ..target import identity_from_window, validate_handle
from ..tracker import Status
from ..win32.capture import Win32Capturer
from ..win32.windows import WindowInfo, Win32WindowSystem, looks_like_studio, selectable_windows

STATUS_COLORS = {
    Status.STOPPED: "#9e9e9e",
    Status.RUNNING: "#2e7d32",
    Status.DEGRADED: "#ef6c00",
    Status.LOST: "#c62828",
}
LIVE_COLORS = {"LIVE": "#c62828", "NOT_LIVE": "#1565c0", "UNKNOWN": "#757575"}
REGION_COLORS = {"detect": "#ffeb3b", "redact": "#f44336", "live": "#00e676"}


def _local(iso_utc: str) -> str:
    if not iso_utc:
        return "-"
    try:
        return datetime.fromisoformat(iso_utc).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso_utc


class SettingsDialog(simpledialog.Dialog):
    def __init__(self, parent, cfg: AppConfig):
        self.cfg = cfg
        super().__init__(parent, "Settings")

    def _entry(self, master, row, label, key, value, secret=False):
        ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=1)
        var = tk.StringVar(value=value)
        ttk.Entry(master, textvariable=var, width=44, show="*" if secret else "").grid(row=row, column=1, padx=4, pady=1)
        self.vars[key] = var

    def _check(self, master, row, label, key, value):
        var = tk.BooleanVar(value=value)
        ttk.Checkbutton(master, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", padx=4)
        self.bools[key] = var

    def body(self, master):
        self.vars, self.bools = {}, {}
        c = self.cfg
        nb = ttk.Notebook(master)
        nb.grid(row=0, column=0, sticky="nsew")

        gen = ttk.Frame(nb, padding=6); nb.add(gen, text="General & Telegram")
        r = 0
        for label, key, value, secret in (
            ("Telegram bot token", "token", c.telegram.bot_token, True),
            ("Telegram chat id", "chat", c.telegram.chat_id, False),
            ("Machine label", "label", c.machine_label, False),
            ("Poll interval (s)", "poll", str(c.detection.poll_interval_seconds), False),
            ("Confirm polls (popups)", "confirm", str(c.detection.confirm_polls), False),
            ("Dedup cooldown (s)", "cooldown", str(c.detection.dedup_cooldown_seconds), False),
            ("Screenshot retention (days)", "retention", str(c.privacy.screenshot_retention_days), False),
            ("Max text chars in alert", "maxtext", str(c.privacy.max_text_in_alert), False),
            ("Popup rules file (blank = bundled)", "rules", c.detection.rules_file, False),
        ):
            self._entry(gen, r, label, key, value, secret); r += 1
        self._check(gen, r, "Attach screenshots to Telegram alerts", "send_shots", c.privacy.send_screenshots); r += 1
        self._check(gen, r, "Store detected text in local incident history", "store_text", c.privacy.store_detected_text); r += 1
        self._check(gen, r, "Also capture separate Studio dialogs/windows", "dialogs", c.detection.include_dialogs); r += 1
        self._check(gen, r, "Send LOST/DEGRADED/RUNNING status changes to Telegram", "notify_status",
                    c.telegram.notify_status_changes); r += 1

        act = ttk.Frame(nb, padding=6); nb.add(act, text="Studio activity")
        a = c.activity
        r = 0
        self._check(act, r, "Notify when Studio opens", "notify_opened", a.notify_opened); r += 1
        self._check(act, r, "Notify when Studio closes", "notify_closed", a.notify_closed); r += 1
        self._check(act, r, "Notify if Studio is already running when monitoring starts", "notify_already",
                    a.notify_already_running); r += 1
        self._check(act, r, "Not-live reminders", "reminders", a.reminders_enabled); r += 1
        self._entry(act, r, "Offline threshold (minutes)", "threshold", str(a.offline_threshold_minutes)); r += 1
        self._check(act, r, "Repeat reminders", "repeat", a.repeat_enabled); r += 1
        self._entry(act, r, "Repeat interval (minutes)", "repeat_interval", str(a.repeat_interval_minutes)); r += 1
        self._entry(act, r, "Maximum repeats", "repeat_max", str(a.repeat_max_count)); r += 1
        self._entry(act, r, "Open screenshot timeout (s)", "open_timeout", str(a.open_screenshot_timeout_seconds)); r += 1
        self._entry(act, r, "Close debounce (s)", "close_debounce", str(a.close_debounce_seconds)); r += 1
        self._entry(act, r, "Max observation gap (s)", "max_gap", str(a.max_observation_gap_seconds)); r += 1
        self._entry(act, r, "Confirm observations (live state)", "confirm_obs", str(a.confirm_observations)); r += 1
        self._entry(act, r, "Live-state rules file (blank = bundled)", "live_rules", a.live_rules_file); r += 1
        self._check(act, r, "Start Monitor Screen when I sign in to Windows (starts monitoring the saved target)",
                    "signin", a.start_at_signin); r += 1
        ttk.Label(act, foreground="#555", wraplength=420, justify="left",
                  text="Studio activity is observed only while the monitor is running. Nothing is reported for "
                       "periods when the monitor was stopped. Use 'Calibrate live state' in the main window to "
                       "check the live-state rules against real Studio screenshots.").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))
        return None

    def validate(self):
        try:
            float(self.vars["poll"].get()); int(self.vars["confirm"].get()); float(self.vars["cooldown"].get())
            int(self.vars["retention"].get()); int(self.vars["maxtext"].get())
            float(self.vars["threshold"].get()); float(self.vars["repeat_interval"].get()); int(self.vars["repeat_max"].get())
            float(self.vars["open_timeout"].get()); float(self.vars["close_debounce"].get())
            float(self.vars["max_gap"].get()); int(self.vars["confirm_obs"].get())
        except ValueError:
            messagebox.showerror("Settings", "Numeric fields must be numbers.")
            return False
        return True

    def apply(self):
        c, v, b = self.cfg, self.vars, self.bools
        c.telegram.bot_token = v["token"].get().strip()
        c.telegram.chat_id = v["chat"].get().strip()
        c.machine_label = v["label"].get().strip() or c.machine_label
        c.detection.poll_interval_seconds = max(0.5, float(v["poll"].get()))
        c.detection.confirm_polls = max(1, int(v["confirm"].get()))
        c.detection.dedup_cooldown_seconds = float(v["cooldown"].get())
        c.privacy.screenshot_retention_days = int(v["retention"].get())
        c.privacy.max_text_in_alert = int(v["maxtext"].get())
        c.detection.rules_file = v["rules"].get().strip()
        c.privacy.send_screenshots = b["send_shots"].get()
        c.privacy.store_detected_text = b["store_text"].get()
        c.detection.include_dialogs = b["dialogs"].get()
        c.telegram.notify_status_changes = b["notify_status"].get()
        a = c.activity
        a.notify_opened = b["notify_opened"].get()
        a.notify_closed = b["notify_closed"].get()
        a.notify_already_running = b["notify_already"].get()
        a.reminders_enabled = b["reminders"].get()
        a.offline_threshold_minutes = max(1.0, float(v["threshold"].get()))
        a.repeat_enabled = b["repeat"].get()
        a.repeat_interval_minutes = max(1.0, float(v["repeat_interval"].get()))
        a.repeat_max_count = max(0, int(v["repeat_max"].get()))
        a.open_screenshot_timeout_seconds = max(1.0, float(v["open_timeout"].get()))
        a.close_debounce_seconds = max(1.0, float(v["close_debounce"].get()))
        a.max_observation_gap_seconds = max(1.0, float(v["max_gap"].get()))
        a.confirm_observations = max(1, int(v["confirm_obs"].get()))
        a.live_rules_file = v["live_rules"].get().strip()
        a.start_at_signin = b["signin"].get()
        self.result = True


class App:
    def __init__(self, root: tk.Tk, cfg: AppConfig, cfg_path: Path, autostart: bool = False) -> None:
        self.root = root
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.system = Win32WindowSystem()
        self.capturer = Win32Capturer()
        self.monitor: Optional[Monitor] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.events: _queue.Queue = _queue.Queue()
        self.windows: list[WindowInfo] = []
        self.preview_image: Optional[Image.Image] = None
        self.preview_scale = 1.0
        self._photo = None
        self._drag_start = None
        self._drag_rect = None
        self._monitor_capture = None
        self._lock = threading.Lock()
        self._history_kind = tk.StringVar(value="all")

        root.title(f"{SOURCE_LABEL} Monitor {__version__}")
        root.geometry("1280x860")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.refresh_windows()
        self._restore_target()
        self.refresh_history()
        self.root.after(200, self._pump_events)
        self.root.after(500, self._refresh_preview)
        if autostart and self.cfg.target.is_set:
            self.root.after(1500, self.start)

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)
        top = ttk.Panedwindow(outer, orient="horizontal")
        top.pack(fill="both", expand=True)

        left = ttk.Labelframe(top, text=f"1. Select your {SOURCE_LABEL} window", padding=6)
        top.add(left, weight=1)
        cols = ("title", "process", "pid", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=12, selectmode="browse")
        for col, text, width in (("title", "Window title", 240), ("process", "Process", 140), ("pid", "PID", 60), ("size", "Size", 80)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("studio", background="#e3f2fd")
        self.tree.pack(fill="both", expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Refresh", command=self.refresh_windows).pack(side="left")
        ttk.Button(btns, text="Use selected window", command=self.use_selected).pack(side="left", padx=4)
        self.target_var = tk.StringVar(value="No target selected.")
        ttk.Label(left, textvariable=self.target_var, wraplength=360, justify="left").pack(fill="x")

        # Studio activity panel
        actf = ttk.Labelframe(left, text="Studio activity", padding=6)
        actf.pack(fill="x", pady=(6, 0))
        self.act_vars: dict[str, tk.StringVar] = {}
        rows = [("Studio application", "app"), ("Broadcast state", "live"), ("Evidence", "evidence"),
                ("Last confirmed observation", "confirmed"), ("Confirmed offline time", "offline"),
                ("Time until reminder", "remaining"), ("Last lifecycle event", "event"),
                ("Notification delivery", "delivery")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(actf, text=label + ":").grid(row=i, column=0, sticky="nw", padx=(0, 6))
            var = tk.StringVar(value="-")
            self.act_vars[key] = var
            lbl = ttk.Label(actf, textvariable=var, wraplength=250, justify="left")
            lbl.grid(row=i, column=1, sticky="w")
            if key == "live":
                self.live_label = lbl
        self.live_label.configure(font=("Segoe UI", 10, "bold"))

        right = ttk.Labelframe(top, text="2. Live preview and regions (drag on the preview)", padding=6)
        top.add(right, weight=2)
        self.canvas = tk.Canvas(right, bg="#202020", width=640, height=380, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        rrow = ttk.Frame(right)
        rrow.pack(fill="x", pady=4)
        self.region_kind = tk.StringVar(value="detect")
        ttk.Radiobutton(rrow, text="Popup detection region", variable=self.region_kind, value="detect").pack(side="left")
        ttk.Radiobutton(rrow, text="Redaction (privacy) region", variable=self.region_kind, value="redact").pack(side="left", padx=6)
        ttk.Radiobutton(rrow, text="Live-status region", variable=self.region_kind, value="live").pack(side="left", padx=6)
        ttk.Button(rrow, text="Remove selected", command=self.remove_region).pack(side="right")
        ttk.Button(rrow, text="Clear all", command=self.clear_regions).pack(side="right", padx=4)
        self.region_list = tk.Listbox(right, height=4)
        self.region_list.pack(fill="x")
        ttk.Label(right, foreground="#555", wraplength=700, justify="left",
                  text="No detection regions = scan the whole window for popups. No live-status regions = classify "
                       "broadcast state from the whole window (less reliable; chat text can look like controls). "
                       "Separate dialogs are always scanned whole.").pack(anchor="w")

        bottom = ttk.Labelframe(outer, text="3. Monitor", padding=6)
        bottom.pack(fill="both", expand=False, pady=(6, 0))
        srow = ttk.Frame(bottom)
        srow.pack(fill="x")
        self.status_label = tk.Label(srow, text="STOPPED", fg="white", bg=STATUS_COLORS[Status.STOPPED],
                                     font=("Segoe UI", 14, "bold"), width=12)
        self.status_label.pack(side="left", padx=(0, 8))
        self.reason_var = tk.StringVar(value="")
        ttk.Label(srow, textvariable=self.reason_var, wraplength=520).pack(side="left", fill="x", expand=True)
        self.start_btn = ttk.Button(srow, text="Start monitoring", command=self.start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(srow, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=4)
        ttk.Button(srow, text="Settings", command=self.open_settings).pack(side="right", padx=4)
        ttk.Button(srow, text="Test Telegram", command=self.test_telegram).pack(side="right", padx=4)
        ttk.Button(srow, text="Calibrate popups", command=self.calibrate).pack(side="right", padx=4)
        ttk.Button(srow, text="Calibrate live state", command=self.calibrate_live).pack(side="right", padx=4)
        self.queue_var = tk.StringVar(value="alert queue: -")
        ttk.Label(bottom, textvariable=self.queue_var, foreground="#555").pack(anchor="w")

        lower = ttk.Panedwindow(bottom, orient="horizontal")
        lower.pack(fill="both", expand=True)
        logf = ttk.Frame(lower)
        lower.add(logf, weight=3)
        self.log = tk.Text(logf, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        histf = ttk.Labelframe(lower, text="History", padding=4)
        lower.add(histf, weight=2)
        hrow = ttk.Frame(histf)
        hrow.pack(fill="x")
        ttk.Label(hrow, text="Show:").pack(side="left")
        for label, value in (("All", "all"), ("Restrictions", "incident"), ("Studio activity", "activity")):
            ttk.Radiobutton(hrow, text=label, variable=self._history_kind, value=value,
                            command=self.refresh_history).pack(side="left", padx=3)
        ttk.Button(hrow, text="Refresh", command=self.refresh_history).pack(side="right")
        self.history = tk.Listbox(histf, height=8, font=("Consolas", 9))
        self.history.pack(fill="both", expand=True)

    # -- helpers ----------------------------------------------------------
    def log_line(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def save(self) -> None:
        try:
            self.cfg.save(self.cfg_path)
        except OSError as exc:
            self.log_line(f"could not save config: {exc}")

    def _set_status(self, status: Status, reason: str = "") -> None:
        self.status_label.configure(text=status.value, bg=STATUS_COLORS[status])
        self.reason_var.set(reason)

    def _show_activity(self, s: ActivitySnapshot) -> None:
        v = self.act_vars
        v["app"].set(f"{s.app_state}" + (f"  (session {s.session_id})" if s.session_id else ""))
        verified = "" if s.live_rules_verified else "  [rules unverified]"
        v["live"].set(s.live_state.replace("_", " ") + verified)
        self.live_label.configure(foreground=LIVE_COLORS.get(s.live_state, "#000"))
        v["evidence"].set((s.live_evidence or s.last_observation or "-")[:160])
        v["confirmed"].set(_local(s.last_confirmed_utc))
        off = format_duration(s.offline_seconds) if s.episode_id else "-"
        if s.episode_id:
            off += "  (counting)" if s.accumulating else "  (paused)"
        v["offline"].set(off)
        if s.remaining_seconds is None:
            rem = "-" if not s.episode_id else ("sent" if s.reminders_sent else "disabled")
        else:
            rem = format_duration(s.remaining_seconds)
        v["remaining"].set(rem)
        v["event"].set(s.last_event or "-")
        d = s.delivery or {}
        c = d.get("counts", {})
        last = d.get("last")
        text = f"pending {c.get('pending', 0)}, sent {c.get('sent', 0)}, failed {c.get('failed', 0)}"
        if last:
            text += f"; last: {last['id']} {last['status']}" + (f" ({last['error'][:60]})" if last.get("error") else "")
        v["delivery"].set(text)
        self.queue_var.set("alert queue: " + text)

    def refresh_history(self) -> None:
        try:
            from ..queue import DeliveryQueue
            q = self.monitor.queue if self.monitor else DeliveryQueue(self.cfg.db_path)
            items = q.history(60, self._history_kind.get())
            if not self.monitor:
                q.close()
        except Exception as exc:
            self.log_line(f"history unavailable: {exc}")
            return
        self.history.delete(0, "end")
        for it in items:
            ts = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
            self.history.insert("end", f"{ts} [{it['kind']}] {it['label']}: {it['detail']}"[:140])

    # -- target selection -------------------------------------------------
    def refresh_windows(self) -> None:
        self.windows = selectable_windows(self.system)
        self.tree.delete(*self.tree.get_children())
        for w in self.windows:
            tags = ("studio",) if looks_like_studio(w) else ()
            self.tree.insert("", "end", iid=str(w.hwnd), values=(w.title, w.exe_name, w.pid,
                             f"{w.rect.width}x{w.rect.height}"), tags=tags)

    def use_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select window", "Pick a window in the list first.")
            return
        hwnd = int(sel[0])
        win = self.system.get_window(hwnd)
        if win is None or not self.system.process_alive(win.pid):
            messagebox.showerror("Select window", "That window is no longer available. Refresh the list.")
            self.refresh_windows()
            return
        self.cfg.target = identity_from_window(win)
        self.save()
        self._show_target()
        self.log_line(f"target set: {win.describe()}; executable discovered at {win.exe_path}")

    def _restore_target(self) -> None:
        if not self.cfg.target.is_set:
            return
        result = validate_handle(self.system, self.cfg.target)
        if not result.ok:
            from ..target import rediscover
            found = rediscover(self.system, self.cfg.target)
            if found is not None:
                self.cfg.target = identity_from_window(found)
                self.save()
                self.log_line(f"stored handle was stale ({result.reason}); rediscovered {found.describe()}")
            else:
                self.log_line(f"stored target not found ({result.reason}); it will be rediscovered when Studio reappears")
        self._show_target()

    def _show_target(self) -> None:
        t = self.cfg.target
        if not t.is_set:
            self.target_var.set("No target selected.")
            return
        self.target_var.set(f'Target: "{t.title}"\nProcess: {t.exe_name} (pid {t.pid}, hwnd 0x{t.hwnd:X})\nPath: {t.exe_path}')
        self._refresh_region_list()

    # -- preview ----------------------------------------------------------
    def _current_window(self) -> Optional[WindowInfo]:
        if not self.cfg.target.is_set:
            return None
        return self.system.get_window(self.cfg.target.hwnd)

    def _refresh_preview(self) -> None:
        try:
            img = None
            with self._lock:
                cap = self._monitor_capture
                self._monitor_capture = None
            if cap is not None:
                img = cap.image
            elif self.monitor is None:
                win = self._current_window()
                if win is not None:
                    fg = self.system.foreground_window()
                    cap = self.capturer.capture(win, fg)
                    if cap is not None:
                        img = cap.image
                    else:
                        self.reason_var.set("Preview unavailable (window minimized or hidden)")
            if img is not None:
                self.preview_image = img
                self._draw_preview()
        except Exception as exc:  # never kill the UI loop
            self.log_line(f"preview error: {exc}")
        self.root.after(700, self._refresh_preview)

    def _draw_preview(self) -> None:
        img = self.preview_image
        if img is None:
            return
        cw, ch = max(50, self.canvas.winfo_width()), max(50, self.canvas.winfo_height())
        scale = min(cw / img.width, ch / img.height, 1.0)
        self.preview_scale = scale
        disp = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        for r in self.cfg.regions:
            l, t, rt, b = r.to_box(disp.width, disp.height)
            color = REGION_COLORS.get(r.kind, "#fff")
            self.canvas.create_rectangle(l, t, rt, b, outline=color, width=2)
            self.canvas.create_text(l + 3, t + 3, anchor="nw", text=r.name, fill=color, font=("Segoe UI", 9, "bold"))

    def _drag_begin(self, event) -> None:
        if self.preview_image is None:
            return
        self._drag_start = (event.x, event.y)
        self._drag_rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00e5ff", width=2, dash=(4, 2))

    def _drag_move(self, event) -> None:
        if self._drag_rect is not None and self._drag_start is not None:
            self.canvas.coords(self._drag_rect, *self._drag_start, event.x, event.y)

    def _drag_end(self, event) -> None:
        if self._drag_rect is None or self._drag_start is None or self.preview_image is None:
            return
        x0, y0 = self._drag_start
        self.canvas.delete(self._drag_rect)
        self._drag_rect = self._drag_start = None
        if abs(event.x - x0) < 8 or abs(event.y - y0) < 8:
            return
        s = self.preview_scale
        box = (int(x0 / s), int(y0 / s), int(event.x / s), int(event.y / s))
        kind = self.region_kind.get()
        n = sum(1 for r in self.cfg.regions if r.kind == kind) + 1
        name = f"{kind} {n}"
        try:
            region = Region.from_pixels(name, box, self.preview_image.size, kind)
        except ValueError as exc:
            self.log_line(f"region rejected: {exc}")
            return
        self.cfg.regions.append(region)
        self.save()
        self._refresh_region_list()
        self._draw_preview()

    def _refresh_region_list(self) -> None:
        self.region_list.delete(0, "end")
        for r in self.cfg.regions:
            self.region_list.insert("end", f"[{r.kind}] {r.name}: x={r.x:.2f} y={r.y:.2f} w={r.w:.2f} h={r.h:.2f}")

    def remove_region(self) -> None:
        sel = self.region_list.curselection()
        if sel:
            del self.cfg.regions[sel[0]]
            self.save()
            self._refresh_region_list()
            self._draw_preview()

    def clear_regions(self) -> None:
        self.cfg.regions.clear()
        self.save()
        self._refresh_region_list()
        self._draw_preview()

    # -- monitoring -------------------------------------------------------
    def start(self) -> None:
        if self.monitor is not None:
            return
        if not self.cfg.target.is_set:
            messagebox.showinfo("Start", f"Select your {SOURCE_LABEL} window first.")
            return
        if not (self.cfg.telegram.bot_token and self.cfg.telegram.chat_id):
            if not messagebox.askyesno("Telegram not configured",
                                       "Telegram is not configured; alerts will queue locally until it is. Start anyway?"):
                return
        try:
            setup_logging(self.cfg)
            self.monitor = build_monitor(
                self.cfg,
                on_event=lambda m: self.events.put(("event", m)),
                on_status=lambda s: self.events.put(("status", s)),
                on_capture=self._on_capture,
                on_identity_change=lambda ident: self.events.put(("identity", ident)),
                on_activity=lambda a: self.events.put(("activity", a)),
            )
        except Exception as exc:
            messagebox.showerror("Start", f"Could not start monitoring:\n{exc}")
            self.monitor = None
            return
        self.monitor_thread = self.monitor.start_background()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def _on_capture(self, cap) -> None:
        with self._lock:
            self._monitor_capture = cap

    def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._set_status(Status.STOPPED, "")
        self.act_vars["app"].set("monitor stopped (not observing)")

    def _pump_events(self) -> None:
        refresh_hist = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "event":
                    self.log_line(payload)
                    if "queued" in payload or "cancelled" in payload:
                        refresh_hist = True
                elif kind == "status":
                    upd: StatusUpdate = payload
                    if self.monitor is not None:
                        self._set_status(upd.status, upd.reason)
                elif kind == "identity":
                    self.cfg.target = payload
                    self.save()
                    self._show_target()
                elif kind == "activity":
                    self._show_activity(payload)
        except _queue.Empty:
            pass
        if refresh_hist:
            self.refresh_history()
        self.root.after(200, self._pump_events)

    # -- misc actions -----------------------------------------------------
    def open_settings(self) -> None:
        before = self.cfg.activity.start_at_signin
        dlg = SettingsDialog(self.root, self.cfg)
        if dlg.result:
            self.save()
            if self.cfg.activity.start_at_signin != before:
                try:
                    from ..startup import apply_setting
                    apply_setting(self.cfg.activity.start_at_signin)
                    self.log_line("start at sign-in " + ("enabled" if self.cfg.activity.start_at_signin else "disabled"))
                except Exception as exc:
                    self.log_line(f"could not update sign-in startup setting: {exc}")
            self.log_line("settings saved" + (" (restart monitoring to apply)" if self.monitor else ""))

    def test_telegram(self) -> None:
        from ..cli import _test_alert
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = _test_alert(self.cfg)
        self.log_line(("test alert sent: " if code == 0 else "test alert failed: ") + buf.getvalue().strip())

    def _pick_image(self, title: str) -> str:
        return filedialog.askopenfilename(title=title,
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])

    def calibrate(self) -> None:
        path = self._pick_image("Choose a real Studio screenshot (popup)")
        if not path:
            return
        try:
            from ..ocr import create_backend
            rules = load_ruleset(self.cfg)
            ocr = create_backend(self.cfg.detection.ocr_backend, self.cfg.detection.ocr_language, self.cfg.detection.ocr_upscale)
            img = Image.open(path)
            regions = [r for r in self.cfg.regions if r.kind == "detect"] or [None]
            for region in regions:
                text = ocr.recognize(region.crop(img) if region else img).text
                name = region.name if region else "full image"
                matches = rules.match_all(text)
                self.log_line(f"calibrate [{name}] OCR: {' '.join(text.split())[:300]!r}")
                if matches:
                    for m in matches:
                        self.log_line(f"calibrate [{name}] MATCH {m.key} ({m.label}) via {m.phrases}")
                else:
                    self.log_line(f"calibrate [{name}] no rule matched; add the wording to the rules file")
        except Exception as exc:
            self.log_line(f"calibrate failed: {exc}")

    def calibrate_live(self) -> None:
        path = self._pick_image("Choose a real Studio screenshot (live or not live)")
        if not path:
            return
        try:
            from ..ocr import create_backend
            live_rules = load_live_rules(self.cfg)
            ocr = create_backend(self.cfg.detection.ocr_backend, self.cfg.detection.ocr_language, self.cfg.detection.ocr_upscale)
            img = Image.open(path)
            regions = self.cfg.live_regions
            text = "\n".join(ocr.recognize(r.crop(img)).text for r in regions) if regions else ocr.recognize(img).text
            c = live_rules.classify(text)
            self.log_line(f"calibrate-live OCR ({'live regions' if regions else 'full image'}): {' '.join(text.split())[:300]!r}")
            self.log_line(f"calibrate-live result: {c.summary()}  live={c.live_score} not_live={c.not_live_score}"
                          f"{'' if live_rules.verified else '  [rules unverified]'}")
        except Exception as exc:
            self.log_line(f"calibrate-live failed: {exc}")

    def on_close(self) -> None:
        self.stop()
        self.save()
        self.root.destroy()


def run_gui(cfg: AppConfig, cfg_path: Path, autostart: bool = False) -> int:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    App(root, cfg, cfg_path, autostart=autostart)
    root.mainloop()
    return 0
