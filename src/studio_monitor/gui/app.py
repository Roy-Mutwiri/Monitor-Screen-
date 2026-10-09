"""Tkinter GUI: Monitor tab (window picker, preview, regions, status, Studio
activity, history with per-bot delivery details) and Telegram Bots tab."""
from __future__ import annotations

import queue as _queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Optional

from PIL import Image, ImageTk

from .. import SOURCE_LABEL, __version__
from ..alerts import format_duration
from ..app import (build_monitor, load_live_rules, load_ruleset, make_capture_service, make_registry, open_queue,
                   run_migrations, setup_logging)
from ..bot_tests import deliver_test_now, enqueue_test, validate_bot, validate_token
from ..bots import EVENT_CATEGORIES, MAX_BOTS, BotError, BotRegistry
from ..config import AppConfig
from ..labels import MAX_OWNER_NAME, OwnerNameError, hostname, validate_owner_name
from ..monitor import ActivitySnapshot, Monitor, StatusUpdate
from ..queue import DeliveryQueue, DeliveryWorker
from ..regions import Region
from ..target import identity_from_window, validate_handle
from ..telegram import ClientFactory, sanitize
from ..tracker import Status
from ..win32.capture import CaptureStatus, Win32Capturer
from ..win32.windows import WindowInfo, Win32WindowSystem, looks_like_studio, selectable_windows

STATUS_COLORS = {
    Status.STOPPED: "#9e9e9e",
    Status.RUNNING: "#2e7d32",
    Status.DEGRADED: "#ef6c00",
    Status.LOST: "#c62828",
}
LIVE_COLORS = {"LIVE": "#c62828", "NOT_LIVE": "#1565c0", "UNKNOWN": "#757575"}
REGION_COLORS = {"detect": "#ffeb3b", "redact": "#f44336", "live": "#00e676"}
TOKEN_HELP = ("Enter the bot token from @BotFather. This is not your Telegram account password or a Telegram "
              "developer API ID/API hash.\nThe token identifies the sending bot; the chat ID identifies the recipient. "
              "Both are required for delivery.")


def _local(iso_utc: str) -> str:
    if not iso_utc:
        return "-"
    try:
        return datetime.fromisoformat(iso_utc).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso_utc


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")


class Background:
    """Run a callable off the Tk thread and deliver its result on the Tk thread."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._results: _queue.Queue = _queue.Queue()
        self.root.after(150, self._pump)

    def run(self, fn: Callable, on_done: Callable[[object, Optional[BaseException]], None]) -> None:
        def _work():
            try:
                self._results.put((on_done, fn(), None))
            except BaseException as exc:  # noqa: BLE001
                self._results.put((on_done, None, exc))
        threading.Thread(target=_work, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                on_done, result, exc = self._results.get_nowait()
                try:
                    on_done(result, exc)
                except Exception:  # pragma: no cover
                    pass
        except _queue.Empty:
            pass
        self.root.after(150, self._pump)


# ---------------------------------------------------------------- settings dialog

class SettingsDialog(simpledialog.Dialog):
    def __init__(self, parent, cfg: AppConfig):
        self.cfg = cfg
        super().__init__(parent, "Settings")

    def _entry(self, master, row, label, key, value):
        ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=1)
        var = tk.StringVar(value=value)
        ttk.Entry(master, textvariable=var, width=44).grid(row=row, column=1, padx=4, pady=1)
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

        gen = ttk.Frame(nb, padding=6); nb.add(gen, text="General")
        r = 0
        for label, key, value in (
            ("Machine label", "label", c.machine_label),
            ("Poll interval (s)", "poll", str(c.detection.poll_interval_seconds)),
            ("Confirm polls (popups)", "confirm", str(c.detection.confirm_polls)),
            ("Dedup cooldown (s)", "cooldown", str(c.detection.dedup_cooldown_seconds)),
            ("Screenshot retention (days)", "retention", str(c.privacy.screenshot_retention_days)),
            ("Max text chars in alert", "maxtext", str(c.privacy.max_text_in_alert)),
            ("Popup rules file (blank = bundled)", "rules", c.detection.rules_file),
            ("Delivery dead-letter age (hours)", "dead_age", str(c.telegram.delivery_max_age_hours)),
            ("Bots delivered in parallel", "concurrency", str(c.telegram.delivery_concurrency)),
            ("Account label in broadcast alerts (optional)", "account", c.account_label),
            ("Capture backend (auto | wgc | printwindow)", "backend", c.capture.backend),
            ("Health alert after degraded for (s)", "degrade_after", str(c.health.degrade_after_seconds)),
            ("Health recovery after stable for (s)", "recover_after", str(c.health.recover_after_seconds)),
        ):
            self._entry(gen, r, label, key, value); r += 1
        self._check(gen, r, "Attach screenshots to Telegram alerts", "send_shots", c.privacy.send_screenshots); r += 1
        self._check(gen, r, "Store detected text in local incident history", "store_text", c.privacy.store_detected_text); r += 1
        self._check(gen, r, "Also capture separate Studio dialogs/windows", "dialogs", c.detection.include_dialogs); r += 1
        ttk.Label(gen, foreground="#555", wraplength=420, justify="left",
                  text="Telegram bots (tokens, destinations, subscriptions) are managed in the Telegram Bots tab "
                       "of the main window.").grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))

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
                       "periods when the monitor was stopped. Use 'Calibrate live state' in the Monitor tab to "
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
            float(self.vars["dead_age"].get()); int(self.vars["concurrency"].get())
            float(self.vars["degrade_after"].get()); float(self.vars["recover_after"].get())
        except ValueError:
            messagebox.showerror("Settings", "Numeric fields must be numbers.")
            return False
        return True

    def apply(self):
        c, v, b = self.cfg, self.vars, self.bools
        c.machine_label = v["label"].get().strip() or c.machine_label
        c.detection.poll_interval_seconds = max(0.5, float(v["poll"].get()))
        c.detection.confirm_polls = max(1, int(v["confirm"].get()))
        c.detection.dedup_cooldown_seconds = float(v["cooldown"].get())
        c.privacy.screenshot_retention_days = int(v["retention"].get())
        c.privacy.max_text_in_alert = int(v["maxtext"].get())
        c.detection.rules_file = v["rules"].get().strip()
        c.telegram.delivery_max_age_hours = max(1.0, float(v["dead_age"].get()))
        c.telegram.delivery_concurrency = max(1, min(10, int(v["concurrency"].get())))
        c.account_label = v["account"].get().strip()
        c.capture.backend = v["backend"].get().strip() or "auto"
        c.health.degrade_after_seconds = max(1.0, float(v["degrade_after"].get()))
        c.health.recover_after_seconds = max(1.0, float(v["recover_after"].get()))
        c.privacy.send_screenshots = b["send_shots"].get()
        c.privacy.store_detected_text = b["store_text"].get()
        c.detection.include_dialogs = b["dialogs"].get()
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


# ---------------------------------------------------------------- bot add/edit dialog

class BotDialog(simpledialog.Dialog):
    """Add or edit a bot. Network calls (getMe) run off the UI thread; the
    dialog stays open until validation finishes or fails."""

    def __init__(self, parent, registry: BotRegistry, factory: ClientFactory, bg: Background, bot=None):
        self.registry = registry
        self.factory = factory
        self.bg = bg
        self.bot = bot
        self.result = None
        self._busy = False
        super().__init__(parent, "Edit bot" if bot else "Add bot")

    def body(self, master):
        b = self.bot
        ttk.Label(master, text=TOKEN_HELP, wraplength=460, justify="left", foreground="#444").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 6))
        ttk.Label(master, text="Bot name").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.name = tk.StringVar(value=b.name if b else "")
        ttk.Entry(master, textvariable=self.name, width=40).grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Label(master, text="Bot API token").grid(row=2, column=0, sticky="w", padx=4, pady=2)
        self.token = tk.StringVar()
        self.token_entry = ttk.Entry(master, textvariable=self.token, width=40, show="•")
        self.token_entry.grid(row=2, column=1, sticky="w", padx=4)
        self._shown = False
        ttk.Button(master, text="Show", width=6, command=self._toggle).grid(row=2, column=2, padx=2)
        if b:
            ttk.Label(master, foreground="#555", wraplength=460, justify="left",
                      text="Leave the token blank to keep the current one. A new token is validated with getMe and "
                           "must belong to the same bot as before (otherwise add it as a new bot).").grid(
                row=3, column=0, columnspan=3, sticky="w", padx=4)
        ttk.Label(master, text="Destination chat ID").grid(row=4, column=0, sticky="w", padx=4, pady=2)
        self.chat = tk.StringVar(value=b.chat_id if b else "")
        ttk.Entry(master, textvariable=self.chat, width=40).grid(row=4, column=1, columnspan=2, sticky="w", padx=4)
        ttk.Label(master, text="Forum topic ID (optional)").grid(row=5, column=0, sticky="w", padx=4, pady=2)
        self.topic = tk.StringVar(value=str(b.thread_id) if b and b.thread_id else "")
        ttk.Entry(master, textvariable=self.topic, width=40).grid(row=5, column=1, columnspan=2, sticky="w", padx=4)
        self.enabled = tk.BooleanVar(value=b.enabled if b else True)
        ttk.Checkbutton(master, text="Enabled", variable=self.enabled).grid(row=6, column=0, columnspan=3, sticky="w", padx=4)
        ttk.Label(master, text="Send this bot:").grid(row=7, column=0, sticky="nw", padx=4, pady=(6, 0))
        subf = ttk.Frame(master)
        subf.grid(row=7, column=1, columnspan=2, sticky="w")
        self.subs: dict[str, tk.BooleanVar] = {}
        current = set(b.subscriptions) if b else set(EVENT_CATEGORIES)
        for i, (key, label) in enumerate(EVENT_CATEGORIES.items()):
            var = tk.BooleanVar(value=key in current)
            ttk.Checkbutton(subf, text=label, variable=var).grid(row=i, column=0, sticky="w")
            self.subs[key] = var
        self.status = tk.StringVar(value="")
        ttk.Label(master, textvariable=self.status, foreground="#1565c0", wraplength=460, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w", padx=4, pady=(6, 0))
        return self.token_entry if not b else None

    def _toggle(self):
        self._shown = not self._shown
        self.token_entry.configure(show="" if self._shown else "•")

    def buttonbox(self):
        box = ttk.Frame(self)
        self.ok_btn = ttk.Button(box, text="Save", width=10, command=self.ok, default="active")
        self.ok_btn.pack(side="left", padx=5, pady=5)
        ttk.Button(box, text="Cancel", width=10, command=self.cancel).pack(side="left", padx=5, pady=5)
        self.bind("<Escape>", self.cancel)
        box.pack()

    def ok(self, event=None):
        if self._busy:
            return
        token = self.token.get().strip()
        subs = [k for k, v in self.subs.items() if v.get()]
        kwargs = dict(name=self.name.get(), chat_id=self.chat.get(), thread_id=self.topic.get(),
                      enabled=self.enabled.get(), subscriptions=subs)
        if self.bot is None and not token:
            self.status.set("A bot token is required.")
            return
        if token:
            self._busy = True
            self.ok_btn.configure(state="disabled")
            self.status.set("Validating token with Telegram (getMe)…")
            self.bg.run(lambda: validate_token(self.factory, token), lambda info, exc: self._validated(info, exc, token, kwargs))
        else:
            self._save(kwargs, None, None)

    def _validated(self, info, exc, token, kwargs):
        self._busy = False
        self.ok_btn.configure(state="normal")
        if exc is not None:
            self.status.set(f"Token validation failed: {sanitize(str(exc))}")
            return
        self.status.set(f"Token valid: @{info['username']} (id {info['id']})")
        self._save(kwargs, token, (info["id"], info["username"]))

    def _save(self, kwargs, token, identity):
        try:
            if self.bot is None:
                self.result = self.registry.add(kwargs["name"], token, kwargs["chat_id"], kwargs["thread_id"],
                                                kwargs["enabled"], kwargs["subscriptions"], identity)
            else:
                self.result = self.registry.update(self.bot.bot_id, name=kwargs["name"], chat_id=kwargs["chat_id"],
                                                   thread_id=kwargs["thread_id"], enabled=kwargs["enabled"],
                                                   subscriptions=kwargs["subscriptions"], new_token=token or None,
                                                   new_token_identity=identity)
        except BotError as exc:
            self.status.set(str(exc))
            return
        except Exception as exc:  # credential store etc.
            self.status.set(f"Could not save: {sanitize(str(exc))}")
            return
        self.withdraw()
        self.update_idletasks()
        self.parent.focus_set()
        self.destroy()


# ---------------------------------------------------------------- main window

class App:
    def __init__(self, root: tk.Tk, cfg: AppConfig, cfg_path: Path, autostart: bool = False) -> None:
        self.root = root
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.system = Win32WindowSystem()
        self.capturer = Win32Capturer(allow_screen_fallback=False, system=self.system)
        self.capture_service = make_capture_service(cfg, self.system)
        self._diag_visible = False
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
        self._history_items: list[dict] = []
        self._detail_rows: list = []
        self.bg = Background(root)

        # Outbox + bot registry are available even while monitoring is stopped.
        self.queue: DeliveryQueue = open_queue(cfg)
        try:
            self.registry: BotRegistry = make_registry(cfg, cfg_path, self.queue)
            self.registry_error = ""
        except Exception as exc:  # credential store unavailable
            from ..credentials import MemoryCredentialStore
            self.registry = BotRegistry(cfg, MemoryCredentialStore(), save=lambda: cfg.save(cfg_path), queue=self.queue)
            self.registry_error = sanitize(str(exc))
        self.factory = ClientFactory(cfg.telegram, self.registry.token_for)
        self.registry.listeners.append(lambda action, bot_id: self.factory.invalidate(bot_id))
        self._migration_notes = run_migrations(cfg, cfg_path, self.registry, self.queue)

        root.title(f"{SOURCE_LABEL} Monitor {__version__}")
        root.geometry("1320x900")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        for note in self._migration_notes:
            self.log_line(note)
        if self.registry_error:
            self.log_line(f"WARNING: secure credential store unavailable ({self.registry_error}); bots cannot be saved")
        self.refresh_windows()
        self._restore_target()
        self.refresh_history()
        self.refresh_bots()
        self.root.after(200, self._pump_events)
        self.root.after(500, self._refresh_preview)
        self.root.after(1000, self._refresh_capture_panel)
        if not any(b.enabled for b in self.registry.bots):
            self.notebook.select(self.bots_tab)
            self.log_line("Setup: add at least one Telegram bot in the Telegram Bots tab, then select the Studio "
                          "window in the Monitor tab and start monitoring.")
        if autostart and self.cfg.target.is_set:
            self.root.after(1500, self.start)

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        self.monitor_tab = ttk.Frame(self.notebook, padding=6)
        self.bots_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(self.monitor_tab, text="  Monitor  ")
        self.notebook.add(self.bots_tab, text="  Telegram Bots  ")
        self._build_monitor_tab(self.monitor_tab)
        self._build_bots_tab(self.bots_tab)

    def _build_monitor_tab(self, outer) -> None:
        owner = ttk.Frame(outer)
        owner.pack(fill="x", pady=(0, 6))
        ttk.Label(owner, text="Whose PC?", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.owner_var = tk.StringVar(value=self.cfg.owner_name)
        entry = ttk.Entry(owner, textvariable=self.owner_var, width=28)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self.save_owner())
        ttk.Button(owner, text="Save", command=self.save_owner).pack(side="left")
        self.owner_label_var = tk.StringVar()
        ttk.Label(owner, textvariable=self.owner_label_var, foreground="#1565c0").pack(side="left", padx=12)
        ttk.Label(owner, text=f"e.g. Roy  \u2192  notifications say \u201cRoy\u2019s Live\u201d (max {MAX_OWNER_NAME} chars; "
                              "blank = machine label)", foreground="#666").pack(side="left")
        self._show_owner_label()

        top = ttk.Panedwindow(outer, orient="horizontal")
        top.pack(fill="both", expand=True)

        left = ttk.Labelframe(top, text=f"1. Select your {SOURCE_LABEL} window", padding=6)
        top.add(left, weight=1)
        cols = ("title", "process", "pid", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", height=10, selectmode="browse")
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

        capf = ttk.Labelframe(left, text="Capture", padding=6)
        capf.pack(fill="x", pady=(6, 0))
        self.cap_vars: dict[str, tk.StringVar] = {}
        for i, (label, key) in enumerate([("Selected window", "window"), ("Capture backend", "backend"),
                                          ("Last valid frame", "frame"), ("Capture health", "health"),
                                          ("Broadcast state", "live2"), ("Last confirmed transition", "transition")]):
            ttk.Label(capf, text=label + ":").grid(row=i, column=0, sticky="nw", padx=(0, 6))
            var = tk.StringVar(value="-")
            self.cap_vars[key] = var
            lbl = ttk.Label(capf, textvariable=var, wraplength=250, justify="left")
            lbl.grid(row=i, column=1, sticky="w")
            if key == "health":
                self.cap_health_label = lbl
        self.diag_btn = ttk.Button(capf, text="Diagnostics \u25b8", command=self._toggle_diag)
        self.diag_btn.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.diag = tk.Text(capf, height=7, width=48, state="disabled", font=("Consolas", 8), wrap="word")

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
        self.canvas = tk.Canvas(right, bg="#202020", width=640, height=360, cursor="crosshair")
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
        self.region_list = tk.Listbox(right, height=3)
        self.region_list.pack(fill="x")
        ttk.Label(right, foreground="#555", wraplength=700, justify="left",
                  text="No detection regions = scan the whole window for popups. No live-status regions = classify "
                       "broadcast state from the whole window (less reliable). Separate dialogs are always scanned whole.").pack(anchor="w")

        bottom = ttk.Labelframe(outer, text="3. Monitor", padding=6)
        bottom.pack(fill="both", expand=False, pady=(6, 0))
        srow = ttk.Frame(bottom)
        srow.pack(fill="x")
        self.status_label = tk.Label(srow, text="STOPPED", fg="white", bg=STATUS_COLORS[Status.STOPPED],
                                     font=("Segoe UI", 14, "bold"), width=12)
        self.status_label.pack(side="left", padx=(0, 8))
        self.reason_var = tk.StringVar(value="")
        ttk.Label(srow, textvariable=self.reason_var, wraplength=480).pack(side="left", fill="x", expand=True)
        self.start_btn = ttk.Button(srow, text="Start monitoring", command=self.start)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(srow, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=4)
        ttk.Button(srow, text="Settings", command=self.open_settings).pack(side="right", padx=4)
        ttk.Button(srow, text="Calibrate popups", command=self.calibrate).pack(side="right", padx=4)
        ttk.Button(srow, text="Calibrate live state", command=self.calibrate_live).pack(side="right", padx=4)
        self.queue_var = tk.StringVar(value="alert queue: -")
        ttk.Label(bottom, textvariable=self.queue_var, foreground="#555").pack(anchor="w")

        lower = ttk.Panedwindow(bottom, orient="horizontal")
        lower.pack(fill="both", expand=True)
        logf = ttk.Frame(lower)
        lower.add(logf, weight=2)
        self.log = tk.Text(logf, height=9, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        histf = ttk.Labelframe(lower, text="History (events and per-bot delivery)", padding=4)
        lower.add(histf, weight=3)
        hrow = ttk.Frame(histf)
        hrow.pack(fill="x")
        ttk.Label(hrow, text="Show:").pack(side="left")
        for label, value in (("All", "all"), ("Restrictions", "incident"), ("Studio activity", "activity")):
            ttk.Radiobutton(hrow, text=label, variable=self._history_kind, value=value,
                            command=self.refresh_history).pack(side="left", padx=3)
        ttk.Button(hrow, text="Refresh", command=self.refresh_history).pack(side="right")
        self.retry_btn = ttk.Button(hrow, text="Retry selected delivery", command=self.retry_selected, state="disabled")
        self.retry_btn.pack(side="right", padx=4)
        self.history = tk.Listbox(histf, height=5, font=("Consolas", 9), exportselection=False)
        self.history.pack(fill="x")
        self.history.bind("<<ListboxSelect>>", lambda e: self._show_event_details())
        self.details = ttk.Treeview(histf, columns=("bot", "dest", "status", "attempts", "msg", "error"),
                                    show="headings", height=4, selectmode="browse")
        for col, text, width in (("bot", "Bot", 110), ("dest", "Destination", 120), ("status", "Status", 70),
                                 ("attempts", "Att.", 40), ("msg", "Msg id", 60), ("error", "Result", 260)):
            self.details.heading(col, text=text)
            self.details.column(col, width=width, anchor="w")
        self.details.pack(fill="both", expand=True)
        self.details.bind("<<TreeviewSelect>>", lambda e: self._update_retry_button())

    def _build_bots_tab(self, outer) -> None:
        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.bots_count = tk.StringVar(value=f"Bots: 0 / {MAX_BOTS}")
        ttk.Label(head, textvariable=self.bots_count, font=("Segoe UI", 12, "bold")).pack(side="left")
        self.bots_limit_note = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.bots_limit_note, foreground="#c62828").pack(side="left", padx=12)
        ttk.Label(outer, text=TOKEN_HELP, wraplength=900, justify="left", foreground="#444").pack(anchor="w", pady=(4, 6))

        cols = ("name", "username", "chat", "enabled", "test", "delivery", "pending")
        self.bots_tree = ttk.Treeview(outer, columns=cols, show="headings", height=11, selectmode="browse")
        for col, text, width in (("name", "Name", 150), ("username", "Telegram bot", 140), ("chat", "Destination", 170),
                                 ("enabled", "Status", 70), ("test", "Last test", 220), ("delivery", "Last delivery", 260),
                                 ("pending", "Pending", 60)):
            self.bots_tree.heading(col, text=text)
            self.bots_tree.column(col, width=width, anchor="w")
        self.bots_tree.pack(fill="both", expand=True)
        self.bots_tree.bind("<<TreeviewSelect>>", lambda e: self._update_bot_buttons())
        brow = ttk.Frame(outer)
        brow.pack(fill="x", pady=6)
        self.add_btn = ttk.Button(brow, text="Add", command=self.add_bot)
        self.add_btn.pack(side="left")
        self.bot_btns = {}
        for text, cmd in (("Edit", self.edit_bot), ("Remove", self.remove_bot), ("Enable/Disable", self.toggle_bot),
                          ("Validate Bot", self.validate_selected_bot), ("Send Test", self.test_selected_bot)):
            b = ttk.Button(brow, text=text, command=cmd, state="disabled")
            b.pack(side="left", padx=4)
            self.bot_btns[text] = b
        ttk.Button(brow, text="Refresh", command=self.refresh_bots).pack(side="right")
        ttk.Label(outer, wraplength=900, justify="left", foreground="#555",
                  text="Validate Bot calls getMe only (nothing is sent) and shows the bot's Telegram identity; it does not "
                       "prove the bot may post to the destination. Send Test sends an explicit test message with a synthetic "
                       "image to this bot's destination only; the desktop is never captured for a test. Tokens are stored in "
                       "the Windows Credential Manager, never in settings, logs or history. Disabling or removing a bot "
                       "cancels its pending deliveries; messages Telegram already accepted cannot be recalled. Delivery is "
                       "at-least-once: after an ambiguous timeout a retry may send a message twice.").pack(anchor="w", pady=(4, 0))
        self.bots_status = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.bots_status, foreground="#1565c0", wraplength=900, justify="left").pack(anchor="w", pady=4)

    # -- owner label -------------------------------------------------------
    def _show_owner_label(self) -> None:
        self.owner_label_var.set(f"Notifications: \u201c{self.cfg.notification_label}\u201d")

    def save_owner(self) -> None:
        try:
            name = validate_owner_name(self.owner_var.get())
        except OwnerNameError as exc:
            messagebox.showerror("Whose PC?", str(exc))
            return
        self.cfg.owner_name = name
        self.owner_var.set(name)
        self.save()
        self._show_owner_label()
        self.log_line(f"owner name saved; new notifications are labelled \u201c{self.cfg.notification_label}\u201d "
                      "(already queued notifications keep their original label)")

    # -- helpers ----------------------------------------------------------
    def log_line(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{time.strftime('%H:%M:%S')}  {sanitize(msg)}\n")
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

    def _toggle_diag(self) -> None:
        self._diag_visible = not self._diag_visible
        if self._diag_visible:
            self.diag.grid(row=7, column=0, columnspan=2, sticky="we")
            self.diag_btn.configure(text="Diagnostics \u25be")
        else:
            self.diag.grid_forget()
            self.diag_btn.configure(text="Diagnostics \u25b8")

    def _show_capture(self, cs: CaptureStatus, health: str = "", reason: str = "", live: str = "",
                      transition: str = "") -> None:
        v = self.cap_vars
        t = self.cfg.target
        v["window"].set(f'"{t.title}" ({t.exe_name}, pid {t.pid})' if t.is_set else "none selected")
        names = {"wgc": "Windows Graphics Capture (window)", "printwindow": "PrintWindow (window)",
                 "desktop-crop": "Desktop crop (explicit fallback)"}
        v["backend"].set(names.get(cs.backend, cs.backend or "-"))
        if cs.last_valid_at:
            age = max(0.0, time.time() - cs.last_valid_at)
            v["frame"].set(f"{datetime.fromtimestamp(cs.last_valid_at):%H:%M:%S} ({age:.0f}s ago, #{cs.frames})")
        else:
            v["frame"].set("none yet")
        h = health or cs.health
        r = reason or cs.reason
        text = {"OK": "OK", "DEGRADED": "Degraded", "NONE": "Not capturing"}.get(h, h)
        v["health"].set(text + (f" \u2014 {r}" if r else ""))
        self.cap_health_label.configure(foreground={"OK": "#2e7d32", "DEGRADED": "#ef6c00"}.get(h, "#555"))
        if live:
            v["live2"].set(live.replace("_", " "))
        if transition:
            v["transition"].set(transition)
        if self._diag_visible:
            d = cs.diagnostics or {}
            lines = [f"backend={cs.backend} code={cs.code or '-'} hwnd=0x{cs.hwnd:X}",
                     f"frames={cs.frames} session_restarts={cs.session_restarts}",
                     f"heartbeat={'alive' if cs.heartbeat_mono and time.monotonic() - cs.heartbeat_mono < 5 else 'stalled'}",
                     f"last_valid_mono_age={(time.monotonic() - cs.last_valid_mono):.1f}s" if cs.last_valid_mono else "last_valid=none"]
            lines += [f"{k}={v_}" for k, v_ in d.items()]
            if t.is_set:
                lines.append(f"target pid={t.pid} start={t.process_start:.0f} class={t.class_name}")
            lines.append(f"hostname={hostname()} machine_label={self.cfg.machine_label} "
                         f"owner={self.cfg.owner_name or '(blank)'} label={self.cfg.notification_label}")
            self.diag.configure(state="normal")
            self.diag.delete("1.0", "end")
            self.diag.insert("end", "\n".join(lines))
            self.diag.configure(state="disabled")

    def _refresh_capture_panel(self) -> None:
        try:
            if self.monitor is None:
                self._show_capture(self.capture_service.status())
        except Exception as exc:  # pragma: no cover
            self.log_line(f"capture panel error: {exc}")
        self.root.after(1000, self._refresh_capture_panel)

    def _show_activity(self, s: ActivitySnapshot) -> None:
        self._show_capture(s.capture, s.health.capture, s.health.capture_reason, s.live_state, s.last_transition)
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
        self._show_delivery(s.delivery)

    def _show_delivery(self, d: dict) -> None:
        c = (d or {}).get("counts", {})
        last = (d or {}).get("last")
        text = (f"pending {c.get('pending', 0)}, sent {c.get('sent', 0)}, failed {c.get('failed', 0)}, "
                f"dead {c.get('dead', 0)}")
        if last:
            text += f"; last: {last['id']} -> {last.get('bot', '?')} {last['status']}"
            if last.get("error"):
                text += f" ({last['error'][:60]})"
        self.act_vars["delivery"].set(text)
        self.queue_var.set("alert queue: " + text)

    # -- history ----------------------------------------------------------
    def refresh_history(self) -> None:
        try:
            self._history_items = self.queue.history(80, self._history_kind.get())
        except Exception as exc:
            self.log_line(f"history unavailable: {exc}")
            return
        self.history.delete(0, "end")
        for it in self._history_items:
            ts = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
            self.history.insert("end", f"{ts} [{it['kind']}] {it['label']}: {it['detail']}"[:160])
        self.details.delete(*self.details.get_children())
        self._detail_rows = []
        self._update_retry_button()
        self._show_delivery(self.queue.delivery_status())

    def _show_event_details(self) -> None:
        sel = self.history.curselection()
        self.details.delete(*self.details.get_children())
        self._detail_rows = []
        if not sel:
            return
        item = self._history_items[sel[0]]
        for d in self.queue.deliveries_for(item["id"]):
            dest = d.chat_id + (f"/{d.thread_id}" if d.thread_id else "")
            self.details.insert("", "end", iid=str(d.id), values=(d.bot_name, dest, d.status, d.attempts,
                                                                 d.message_id or "-", d.last_error[:120]))
            self._detail_rows.append(d)
        self._update_retry_button()

    def _update_retry_button(self) -> None:
        sel = self.details.selection()
        ok = False
        if sel:
            d = next((x for x in self._detail_rows if str(x.id) == sel[0]), None)
            ok = d is not None and d.status in ("failed", "dead", "cancelled")
        self.retry_btn.configure(state="normal" if ok else "disabled")

    def retry_selected(self) -> None:
        sel = self.details.selection()
        if not sel:
            return
        if self.queue.retry_delivery(int(sel[0])):
            self.log_line(f"delivery {sel[0]} re-queued (only this bot; successful bots are not resent)")
            if self.monitor is not None:
                self.monitor.worker and self.monitor.worker.kick()
            else:
                self._deliver_pending_once()
        self._show_event_details()

    def _deliver_pending_once(self) -> None:
        """Without a running monitor, push due deliveries once in the background."""
        worker = DeliveryWorker(self.queue, self._send_without_monitor, concurrency=self.cfg.telegram.delivery_concurrency,
                                on_event=lambda m: self.events.put(("event", m)))
        self.bg.run(lambda: worker.process_round(parallel=False), lambda r, e: self.refresh_history())

    def _send_without_monitor(self, d):
        from ..queue import DeliveryError
        from ..telegram import deliver
        client = self.factory.client(d.bot_id, d.chat_id, d.thread_id)
        if client is None:
            raise DeliveryError("bot token not available in the credential store", permanent=True)
        result = deliver(client, d.payload, d.evidence_path)
        return result.get("message_id") if isinstance(result, dict) else None

    # -- bots tab ---------------------------------------------------------
    def refresh_bots(self) -> None:
        reg = self.registry
        self.bots_count.set(f"Bots: {reg.count} / {MAX_BOTS}")
        self.bots_tree.delete(*self.bots_tree.get_children())
        for b in reg.bots:
            st = self.queue.bot_stats(b.bot_id)
            last = st["last_result"] or "-"
            if st["blocked_until"] and st["blocked_until"] > time.time():
                last = f"rate-limited until {_fmt_ts(st['blocked_until'])}; " + last
            self.bots_tree.insert("", "end", iid=b.bot_id, values=(
                b.name, ("@" + b.verified_username) if b.verified_username else "not validated", b.destination,
                "enabled" if b.enabled else "disabled",
                (b.last_test_result + (f" ({_fmt_ts_iso(b.last_test_utc)})" if b.last_test_utc else "")) if b.last_test_result else "-",
                last[:120], st["pending"]))
        if reg.can_add:
            self.add_btn.configure(state="normal")
            self.bots_limit_note.set("")
        else:
            self.add_btn.configure(state="disabled")
            self.bots_limit_note.set(f"Limit reached: at most {MAX_BOTS} bots (disabled bots count). Remove one to add another.")
        self._update_bot_buttons()

    def _selected_bot(self):
        sel = self.bots_tree.selection()
        return self.registry.get(sel[0]) if sel else None

    def _update_bot_buttons(self) -> None:
        state = "normal" if self._selected_bot() else "disabled"
        for b in self.bot_btns.values():
            b.configure(state=state)

    def add_bot(self) -> None:
        if not self.registry.can_add:
            messagebox.showinfo("Bots", f"At most {MAX_BOTS} bots can be saved (disabled bots count).")
            return
        dlg = BotDialog(self.root, self.registry, self.factory, self.bg)
        if dlg.result is not None:
            self.log_line(f"bot '{dlg.result.name}' added -> {dlg.result.destination}")
        self.refresh_bots()

    def edit_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        dlg = BotDialog(self.root, self.registry, self.factory, self.bg, bot=bot)
        if dlg.result is not None:
            self.log_line(f"bot '{bot.name}' updated (destination changes apply to future events only)")
        self.refresh_bots()

    def remove_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        pend = self.queue.bot_stats(bot.bot_id)["pending"]
        if not messagebox.askyesno("Remove bot", f"Remove '{bot.name}' ({bot.destination})?\n\nIts {pend} pending "
                                                 f"delivery(ies) will be cancelled and its stored token deleted. "
                                                 f"History is kept."):
            return
        try:
            self.registry.remove(bot.bot_id)
            self.log_line(f"bot '{bot.name}' removed")
        except BotError as exc:
            self.log_line(str(exc))
        self.refresh_bots()
        self.refresh_history()

    def toggle_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        try:
            self.registry.set_enabled(bot.bot_id, not bot.enabled)
            self.log_line(f"bot '{bot.name}' {'enabled' if bot.enabled else 'disabled (pending deliveries cancelled)'}")
        except BotError as exc:
            self.log_line(str(exc))
        self.refresh_bots()
        self.refresh_history()

    def validate_selected_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        self.bots_status.set(f"Validating '{bot.name}' with getMe… (nothing is sent)")

        def done(info, exc):
            if exc is not None:
                self.bots_status.set(f"'{bot.name}': token validation failed: {sanitize(str(exc))}")
                try:
                    self.registry.record_test(bot.bot_id, f"validation failed: {sanitize(str(exc))}")
                except BotError:
                    pass
            else:
                self.bots_status.set(f"'{bot.name}' token is valid: @{info['username']} (id {info['id']}). "
                                     "Use Send Test to check that it can post to the destination.")
            self.refresh_bots()
        self.bg.run(lambda: validate_bot(self.factory, self.registry, bot.bot_id), done)

    def test_selected_bot(self) -> None:
        bot = self._selected_bot()
        if bot is None:
            return
        self.bots_status.set(f"Sending a synthetic test notification to '{bot.name}' ({bot.destination})…")

        def work():
            eid = enqueue_test(self.queue, self.registry, self.cfg, bot.bot_id)
            return deliver_test_now(self.queue, self.registry, self.factory, eid)

        def done(res, exc):
            self.bots_status.set(f"'{bot.name}': {res if exc is None else sanitize(str(exc))}")
            self.refresh_bots()
            self.refresh_history()
        self.bg.run(work, done)

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
        self.cfg.target = identity_from_window(win, self.system)
        self.save()
        self._show_target()
        self.capture_service.bind(win.hwnd)
        self.log_line(f"target set: {win.describe()}; executable discovered at {win.exe_path}; capture initialized "
                      f"({'window capture' if self.capture_service.wgc_available else 'PrintWindow'})")

    def _restore_target(self) -> None:
        if not self.cfg.target.is_set:
            return
        result = validate_handle(self.system, self.cfg.target)
        if not result.ok:
            from ..target import rediscover
            found = rediscover(self.system, self.cfg.target)
            if found is not None:
                self.cfg.target = identity_from_window(found, self.system)
                self.save()
                self.log_line(f"stored handle was stale ({result.reason}); rediscovered {found.describe()}")
            else:
                self.log_line(f"stored target not found ({result.reason}); it will be rediscovered when Studio reappears")
        if validate_handle(self.system, self.cfg.target).ok:
            self.capture_service.bind(self.cfg.target.hwnd)
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
            cap = self.capture_service.frame(max_age=float("inf"))
            if cap is not None and cap.hwnd == self.cfg.target.hwnd:
                img = cap.image
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
        if not any(b.enabled for b in self.registry.bots):
            if not messagebox.askyesno("No Telegram bots",
                                       "No enabled Telegram bot is configured; events will be recorded with no "
                                       "deliveries. Start anyway?"):
                return
        try:
            setup_logging(self.cfg)
            self.monitor = build_monitor(
                self.cfg, self.cfg_path, registry=self.registry, queue=self.queue,
                on_event=lambda m: self.events.put(("event", m)),
                on_status=lambda s: self.events.put(("status", s)),
                on_capture=self._on_capture,
                on_identity_change=lambda ident: self.events.put(("identity", ident)),
                on_activity=lambda a: self.events.put(("activity", a)),
                frame_service=self.capture_service,
            )
            self.monitor.client_factory = self.factory
        except Exception as exc:
            messagebox.showerror("Start", f"Could not start monitoring:\n{sanitize(str(exc))}")
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
        if self.cfg.target.is_set and self.cfg.target.hwnd:
            self.capture_service.bind(self.cfg.target.hwnd)   # keep the preview of the selected window alive

    def _pump_events(self) -> None:
        refresh_hist = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "event":
                    self.log_line(payload)
                    if "queued" in payload or "cancelled" in payload or "delivered" in payload or "failed" in payload:
                        refresh_hist = True
                elif kind == "status":
                    upd: StatusUpdate = payload
                    if self.monitor is not None:
                        self._set_status(upd.status, upd.reason)
                elif kind == "identity":
                    self.cfg.target = payload
                    self.save()
                    self._show_target()
                    self.capture_service.bind(payload.hwnd)
                elif kind == "activity":
                    self._show_activity(payload)
        except _queue.Empty:
            pass
        if refresh_hist:
            self.refresh_history()
            self.refresh_bots()
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
        try:
            self.capture_service.stop()
        except Exception:
            pass
        self.save()
        try:
            self.queue.close()
        except Exception:
            pass
        self.root.destroy()


def _fmt_ts_iso(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso


def run_gui(cfg: AppConfig, cfg_path: Path, autostart: bool = False) -> int:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    App(root, cfg, cfg_path, autostart=autostart)
    root.mainloop()
    return 0
