"""Tkinter GUI: pick the Studio window, preview it, draw regions, monitor."""
from __future__ import annotations

import queue as _queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Optional

from PIL import Image, ImageTk

from .. import SOURCE_LABEL, __version__
from ..app import build_monitor, load_ruleset, setup_logging
from ..config import AppConfig
from ..monitor import Monitor, StatusUpdate
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


class SettingsDialog(simpledialog.Dialog):
    def __init__(self, parent, cfg: AppConfig):
        self.cfg = cfg
        super().__init__(parent, "Settings")

    def body(self, master):
        self.vars = {}
        rows = [
            ("Telegram bot token", "token", self.cfg.telegram.bot_token, True),
            ("Telegram chat id", "chat", self.cfg.telegram.chat_id, False),
            ("Machine label", "label", self.cfg.machine_label, False),
            ("Poll interval (s)", "poll", str(self.cfg.detection.poll_interval_seconds), False),
            ("Confirm polls", "confirm", str(self.cfg.detection.confirm_polls), False),
            ("Dedup cooldown (s)", "cooldown", str(self.cfg.detection.dedup_cooldown_seconds), False),
            ("Screenshot retention (days)", "retention", str(self.cfg.privacy.screenshot_retention_days), False),
            ("Max text chars in alert", "maxtext", str(self.cfg.privacy.max_text_in_alert), False),
            ("Rules file (blank = bundled)", "rules", self.cfg.detection.rules_file, False),
        ]
        for i, (label, key, value, secret) in enumerate(rows):
            ttk.Label(master, text=label).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            var = tk.StringVar(value=value)
            ttk.Entry(master, textvariable=var, width=48, show="*" if secret else "").grid(row=i, column=1, padx=4, pady=2)
            self.vars[key] = var
        self.send_shots = tk.BooleanVar(value=self.cfg.privacy.send_screenshots)
        ttk.Checkbutton(master, text="Attach screenshots to Telegram alerts", variable=self.send_shots).grid(
            row=len(rows), column=0, columnspan=2, sticky="w", padx=4)
        self.store_text = tk.BooleanVar(value=self.cfg.privacy.store_detected_text)
        ttk.Checkbutton(master, text="Store detected text in local incident history", variable=self.store_text).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=4)
        self.dialogs = tk.BooleanVar(value=self.cfg.detection.include_dialogs)
        ttk.Checkbutton(master, text="Also capture separate Studio dialogs/windows", variable=self.dialogs).grid(
            row=len(rows) + 2, column=0, columnspan=2, sticky="w", padx=4)
        self.notify_status = tk.BooleanVar(value=self.cfg.telegram.notify_status_changes)
        ttk.Checkbutton(master, text="Send LOST/DEGRADED/RUNNING status changes to Telegram",
                        variable=self.notify_status).grid(row=len(rows) + 3, column=0, columnspan=2, sticky="w", padx=4)
        return None

    def validate(self):
        try:
            float(self.vars["poll"].get()); int(self.vars["confirm"].get())
            float(self.vars["cooldown"].get()); int(self.vars["retention"].get()); int(self.vars["maxtext"].get())
        except ValueError:
            messagebox.showerror("Settings", "Numeric fields must be numbers.")
            return False
        return True

    def apply(self):
        c = self.cfg
        c.telegram.bot_token = self.vars["token"].get().strip()
        c.telegram.chat_id = self.vars["chat"].get().strip()
        c.machine_label = self.vars["label"].get().strip() or c.machine_label
        c.detection.poll_interval_seconds = max(0.5, float(self.vars["poll"].get()))
        c.detection.confirm_polls = max(1, int(self.vars["confirm"].get()))
        c.detection.dedup_cooldown_seconds = float(self.vars["cooldown"].get())
        c.privacy.screenshot_retention_days = int(self.vars["retention"].get())
        c.privacy.max_text_in_alert = int(self.vars["maxtext"].get())
        c.detection.rules_file = self.vars["rules"].get().strip()
        c.privacy.send_screenshots = self.send_shots.get()
        c.privacy.store_detected_text = self.store_text.get()
        c.detection.include_dialogs = self.dialogs.get()
        c.telegram.notify_status_changes = self.notify_status.get()
        self.result = True


class App:
    def __init__(self, root: tk.Tk, cfg: AppConfig, cfg_path: Path) -> None:
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
        self._last_preview_at = 0.0
        self._monitor_capture = None
        self._lock = threading.Lock()

        root.title(f"{SOURCE_LABEL} Monitor {__version__}")
        root.geometry("1180x760")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.refresh_windows()
        self._restore_target()
        self.root.after(200, self._pump_events)
        self.root.after(500, self._refresh_preview)

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)
        top = ttk.Panedwindow(outer, orient="horizontal")
        top.pack(fill="both", expand=True)

        left = ttk.Labelframe(top, text=f"1. Select your {SOURCE_LABEL} window", padding=6)
        top.add(left, weight=1)
        cols = ("title", "process", "pid", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=14, selectmode="browse")
        for col, text, width in (("title", "Window title", 260), ("process", "Process", 150), ("pid", "PID", 60), ("size", "Size", 80)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("studio", background="#e3f2fd")
        self.tree.pack(fill="both", expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Refresh", command=self.refresh_windows).pack(side="left")
        ttk.Button(btns, text="Use selected window", command=self.use_selected).pack(side="left", padx=4)
        self.target_var = tk.StringVar(value="No target selected.")
        ttk.Label(left, textvariable=self.target_var, wraplength=380, justify="left").pack(fill="x")

        right = ttk.Labelframe(top, text="2. Live preview and detection regions (drag on the preview)", padding=6)
        top.add(right, weight=2)
        self.canvas = tk.Canvas(right, bg="#202020", width=640, height=380, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        rrow = ttk.Frame(right)
        rrow.pack(fill="x", pady=4)
        self.region_kind = tk.StringVar(value="detect")
        ttk.Radiobutton(rrow, text="Draw detection region", variable=self.region_kind, value="detect").pack(side="left")
        ttk.Radiobutton(rrow, text="Draw redaction (privacy) region", variable=self.region_kind, value="redact").pack(side="left", padx=8)
        ttk.Button(rrow, text="Remove selected", command=self.remove_region).pack(side="right")
        ttk.Button(rrow, text="Clear all", command=self.clear_regions).pack(side="right", padx=4)
        self.region_list = tk.Listbox(right, height=4)
        self.region_list.pack(fill="x")
        ttk.Label(right, text="No detection regions = scan the whole window. Separate dialogs are always scanned whole.",
                  foreground="#555").pack(anchor="w")

        bottom = ttk.Labelframe(outer, text="3. Monitor", padding=6)
        bottom.pack(fill="both", expand=False, pady=(6, 0))
        srow = ttk.Frame(bottom)
        srow.pack(fill="x")
        self.status_label = tk.Label(srow, text="STOPPED", fg="white", bg=STATUS_COLORS[Status.STOPPED],
                                     font=("Segoe UI", 14, "bold"), width=12)
        self.status_label.pack(side="left", padx=(0, 8))
        self.reason_var = tk.StringVar(value="")
        ttk.Label(srow, textvariable=self.reason_var, wraplength=600).pack(side="left", fill="x", expand=True)
        self.start_btn = ttk.Button(srow, text="Start monitoring", command=self.start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(srow, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=4)
        ttk.Button(srow, text="Settings", command=self.open_settings).pack(side="right", padx=4)
        ttk.Button(srow, text="Test Telegram", command=self.test_telegram).pack(side="right", padx=4)
        ttk.Button(srow, text="Calibrate from screenshot", command=self.calibrate).pack(side="right", padx=4)
        self.queue_var = tk.StringVar(value="queue: -")
        ttk.Label(bottom, textvariable=self.queue_var, foreground="#555").pack(anchor="w")
        self.log = tk.Text(bottom, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

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
            color = "#ffeb3b" if r.kind == "detect" else "#f44336"
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
        name = f"{'detect' if kind == 'detect' else 'redact'} {n}"
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

    def _pump_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "event":
                    self.log_line(payload)
                elif kind == "status":
                    upd: StatusUpdate = payload
                    if self.monitor is not None:
                        self._set_status(upd.status, upd.reason)
                    if upd.queue_counts:
                        q = upd.queue_counts
                        self.queue_var.set(f"alert queue: pending {q.get('pending', 0)}  sent {q.get('sent', 0)}  failed {q.get('failed', 0)}")
                elif kind == "identity":
                    self.cfg.target = payload
                    self.save()
                    self._show_target()
        except _queue.Empty:
            pass
        self.root.after(200, self._pump_events)

    # -- misc actions -----------------------------------------------------
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.root, self.cfg)
        if dlg.result:
            self.save()
            self.log_line("settings saved" + (" (restart monitoring to apply)" if self.monitor else ""))

    def test_telegram(self) -> None:
        from ..cli import _test_alert
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = _test_alert(self.cfg)
        self.log_line(("test alert sent: " if code == 0 else "test alert failed: ") + buf.getvalue().strip())

    def calibrate(self) -> None:
        path = filedialog.askopenfilename(title="Choose a real Studio screenshot",
                                          filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
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

    def on_close(self) -> None:
        self.stop()
        self.save()
        self.root.destroy()


def run_gui(cfg: AppConfig, cfg_path: Path) -> int:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    App(root, cfg, cfg_path)
    root.mainloop()
    return 0
