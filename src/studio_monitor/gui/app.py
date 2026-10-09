"""Desktop interface (Tkinter + ttkbootstrap 2).

Layout: header (brand, owner label, live status pills, Start/Stop), sidebar
navigation, pages (Monitor, Telegram Bots, History, Settings, Diagnostics)
and a status bar. Typography follows the Windows type ramp (Segoe UI
Variable); icons are Bootstrap Icons rendered by ttkbootstrap, not emoji.
All network and capture work stays off the Tk thread.
"""
from __future__ import annotations

import os
import queue as _queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import Callable, Optional

import ttkbootstrap as tb
from PIL import Image, ImageTk
from ttkbootstrap import Fonts, Icon, ScrolledFrame, ScrolledText, ToolTip

from .. import SOURCE_LABEL, __version__
from ..alerts import format_duration
from ..app import (enroll_agent, build_monitor, load_live_rules, load_ruleset, make_capture_service, make_registry, open_queue,
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

THEMES = {"Dark": "bootstrap-dark", "Light": "bootstrap-light"}
PAGES = [("monitor", "Monitor", "display"), ("bots", "Telegram Bots", "robot"), ("history", "History", "clock-history"),
         ("settings", "Settings", "gear-fill"), ("diagnostics", "Diagnostics", "activity")]
STATUS_STYLE = {Status.STOPPED: "secondary", Status.RUNNING: "success", Status.DEGRADED: "warning", Status.LOST: "danger"}
LIVE_STYLE = {"LIVE": "danger", "NOT_LIVE": "info", "UNKNOWN": "secondary"}
HEALTH_STYLE = {"OK": "success", "DEGRADED": "warning", "NONE": "secondary"}
REGION_COLORS = {"detect": "#ffcd39", "redact": "#e35d6a", "live": "#479f76", "profile": "#3dd5f3",
                 "face": "#d63384", "audio": "#6f42c1"}
BACKEND_NAMES = {"wgc": "Windows Graphics Capture", "printwindow": "PrintWindow", "desktop-crop": "Desktop crop (fallback)"}
TOKEN_HELP = ("Enter the bot token from @BotFather. This is not your Telegram account password or a Telegram "
              "developer API ID/API hash. The token identifies the sending bot; the chat ID identifies the recipient. "
              "Both are required for delivery.")


def ico(name: str, size: int = 16, color: str = "fg"):
    """Bootstrap icon as a Tk image (None if the glyph is unavailable)."""
    try:
        return Icon(name, size=size, color=color)
    except Exception:
        return None


def _local(iso_utc: str) -> str:
    if not iso_utc:
        return "-"
    try:
        return datetime.fromisoformat(iso_utc).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso_utc


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S") if ts else "-"


def _sched_set(cfg: AppConfig, key: str, value) -> None:
    s = cfg.schedule
    setattr(s, key, value)
    cfg.device.schedule = s.to_dict()


def _fmt_ts_iso(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso


def setup_typography() -> dict:
    """Windows type ramp on Segoe UI Variable: body 10pt, caption 9pt, subtitle 12pt semibold, title 16pt."""
    try:
        Fonts.set_global_family("Segoe UI Variable Text", mono_family="Cascadia Mono")
    except Exception:
        pass
    fams = set(tkfont.families())
    body = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
    display = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else body
    mono = "Cascadia Mono" if "Cascadia Mono" in fams else "Consolas"
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=body, size=10)
        except tk.TclError:
            pass
    return {
        "title": tkfont.Font(family=display, size=16, weight="bold"),
        "subtitle": tkfont.Font(family=display, size=12, weight="bold"),
        "strong": tkfont.Font(family=body, size=10, weight="bold"),
        "body": tkfont.Font(family=body, size=10),
        "caption": tkfont.Font(family=body, size=9),
        "value": tkfont.Font(family=display, size=14, weight="bold"),
        "mono": tkfont.Font(family=mono, size=9),
    }


class Background:
    """Run a callable off the Tk thread and deliver its result on the Tk thread."""

    def __init__(self, root: tk.Misc) -> None:
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
        try:
            self.root.after(150, self._pump)
        except tk.TclError:
            pass


class Pill(tb.Label):
    """Compact status badge: ``set(text, style)``."""

    def __init__(self, master, text: str = "-", style: str = "secondary", **kw):
        super().__init__(master, text=text, bootstyle=f"@{style}", padding=(10, 3), **kw)

    def set(self, text: str, style: str) -> None:
        self.configure(text=text, bootstyle=f"@{style}")


class Tile(tb.Labelframe):
    """Stat tile: big value, caption, optional detail line."""

    def __init__(self, master, title: str, fonts: dict):
        super().__init__(master, text=f"  {title}", padding=(12, 8))
        self.value = tb.Label(self, text="-", font=fonts["value"])
        self.value.pack(anchor="w")
        self.caption = tb.Label(self, text="", font=fonts["caption"], bootstyle="secondary", wraplength=230, justify="left")
        self.caption.pack(anchor="w")
        self.detail = tb.Label(self, text="", font=fonts["caption"], wraplength=230, justify="left")
        self.detail.pack(anchor="w")

    def set(self, value: str, caption: str = "", detail: str = "", style: str = "") -> None:
        self.value.configure(text=value, bootstyle=style or "default")
        self.caption.configure(text=caption)
        self.detail.configure(text=detail)


# ---------------------------------------------------------------- bot add/edit dialog

class EnrollDialog(simpledialog.Dialog):
    """Hub URL + single-use pairing code + delivery mode."""

    def __init__(self, parent, url: str = "", mode: str = "standalone"):
        self.result = None
        self._url, self._mode = url, mode
        super().__init__(parent, "Enroll with the fleet hub")

    def body(self, master):
        tb.Label(master, text="Hub URL").grid(row=0, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar(value=self._url)
        tb.Entry(master, textvariable=self.url_var, width=46).grid(row=0, column=1, pady=4)
        tb.Label(master, text="Pairing code").grid(row=1, column=0, sticky="w", pady=4)
        self.code_var = tk.StringVar()
        e = tb.Entry(master, textvariable=self.code_var, width=46)
        e.grid(row=1, column=1, pady=4)
        tb.Label(master, text="Delivery mode").grid(row=2, column=0, sticky="w", pady=4)
        self.mode_var = tk.StringVar(value=self._mode)
        tb.Combobox(master, textvariable=self.mode_var, values=["standalone", "managed"], state="readonly", width=20).grid(row=2, column=1, sticky="w", pady=4)
        tb.Label(master, text="The code is single use and expires. The hub issues this PC a private credential stored in the "
                              "Windows Credential Manager; it is never written to settings.", wraplength=420, justify="left",
                 bootstyle="secondary").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        return e

    def validate(self):
        if not self.url_var.get().strip().startswith(("http://", "https://")):
            messagebox.showerror("Hub URL", "Enter the hub URL including http:// or https://", parent=self)
            return False
        if len(self.code_var.get().replace("-", "").strip()) < 8:
            messagebox.showerror("Pairing code", "Enter the pairing code shown by the hub.", parent=self)
            return False
        return True

    def apply(self):
        self.result = (self.url_var.get().strip(), self.code_var.get().strip(), self.mode_var.get())


class BotDialog(simpledialog.Dialog):
    """Add or edit a bot. getMe runs off the UI thread; the dialog stays open
    until validation finishes or fails."""

    def __init__(self, parent, registry: BotRegistry, factory: ClientFactory, bg: Background, bot=None):
        self.registry, self.factory, self.bg, self.bot = registry, factory, bg, bot
        self.result = None
        self._busy = False
        super().__init__(parent, "Edit bot" if bot else "Add bot")

    def body(self, master):
        b = self.bot
        tb.Label(master, text=TOKEN_HELP, wraplength=470, justify="left", bootstyle="secondary").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        tb.Label(master, text="Bot name").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        self.name = tk.StringVar(value=b.name if b else "")
        tb.Entry(master, textvariable=self.name, width=40).grid(row=1, column=1, columnspan=2, sticky="w", padx=4)
        tb.Label(master, text="Bot API token").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        self.token = tk.StringVar()
        self.token_entry = tb.Entry(master, textvariable=self.token, width=40, show="•")
        self.token_entry.grid(row=2, column=1, sticky="w", padx=4)
        self._shown = False
        self.show_btn = tb.Button(master, image=ico("eye"), command=self._toggle, bootstyle="secondary-outline", width=3)
        self.show_btn.grid(row=2, column=2, padx=2)
        ToolTip(self.show_btn, text="Show / hide token")
        if b:
            tb.Label(master, bootstyle="secondary", wraplength=470, justify="left",
                     text="Leave the token blank to keep the current one. A new token is validated with getMe and must "
                          "belong to the same bot as before (otherwise add it as a new bot).").grid(
                row=3, column=0, columnspan=3, sticky="w", padx=4)
        tb.Label(master, text="Destination chat ID").grid(row=4, column=0, sticky="w", padx=4, pady=3)
        self.chat = tk.StringVar(value=b.chat_id if b else "")
        tb.Entry(master, textvariable=self.chat, width=40).grid(row=4, column=1, columnspan=2, sticky="w", padx=4)
        tb.Label(master, text="Forum topic ID (optional)").grid(row=5, column=0, sticky="w", padx=4, pady=3)
        self.topic = tk.StringVar(value=str(b.thread_id) if b and b.thread_id else "")
        tb.Entry(master, textvariable=self.topic, width=40).grid(row=5, column=1, columnspan=2, sticky="w", padx=4)
        self.enabled = tk.BooleanVar(value=b.enabled if b else True)
        tb.Checkbutton(master, text="Enabled", variable=self.enabled, bootstyle="round-toggle").grid(
            row=6, column=0, columnspan=3, sticky="w", padx=4, pady=4)
        tb.Label(master, text="Send this bot:").grid(row=7, column=0, sticky="nw", padx=4, pady=(6, 0))
        subf = tb.Frame(master)
        subf.grid(row=7, column=1, columnspan=2, sticky="w")
        self.subs: dict[str, tk.BooleanVar] = {}
        current = set(b.subscriptions) if b else set(EVENT_CATEGORIES)
        for i, (key, label) in enumerate(EVENT_CATEGORIES.items()):
            var = tk.BooleanVar(value=key in current)
            tb.Checkbutton(subf, text=label, variable=var).grid(row=i, column=0, sticky="w", pady=1)
            self.subs[key] = var
        self.status = tk.StringVar(value="")
        tb.Label(master, textvariable=self.status, bootstyle="info", wraplength=470, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w", padx=4, pady=(8, 0))
        return self.token_entry if not b else None

    def _toggle(self):
        self._shown = not self._shown
        self.token_entry.configure(show="" if self._shown else "•")
        self.show_btn.configure(image=ico("eye-slash" if self._shown else "eye"))

    def buttonbox(self):
        box = tb.Frame(self)
        self.ok_btn = tb.Button(box, text="Save", width=10, command=self.ok, bootstyle="primary",
                                image=ico("save2", color="light"), compound="left")
        self.ok_btn.pack(side="left", padx=5, pady=8)
        tb.Button(box, text="Cancel", width=10, command=self.cancel, bootstyle="secondary-outline").pack(side="left", padx=5, pady=8)
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
            self.bg.run(lambda: validate_token(self.factory, token),
                        lambda info, exc: self._validated(info, exc, token, kwargs))
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
        except Exception as exc:
            self.status.set(f"Could not save: {sanitize(str(exc))}")
            return
        self.withdraw()
        self.update_idletasks()
        self.parent.focus_set()
        self.destroy()


# ---------------------------------------------------------------- main application

class App:
    def __init__(self, root: tk.Misc, cfg: AppConfig, cfg_path: Path, autostart: bool = False) -> None:
        self.root = root
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.style = tb.Style()
        try:
            self.style.theme_use(cfg.ui.theme)
        except Exception:
            pass
        self.fonts = setup_typography()
        self.system = Win32WindowSystem()
        self.capturer = Win32Capturer(allow_screen_fallback=False, system=self.system)
        self.capture_service = make_capture_service(cfg, self.system)
        self.monitor: Optional[Monitor] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.events: _queue.Queue = _queue.Queue()
        self.windows: list[WindowInfo] = []
        self.preview_image: Optional[Image.Image] = None
        self.preview_scale = 1.0
        self._photo = None
        self._drag_start = None
        self._drag_rect = None
        self._history_items: list[dict] = []
        self._detail_rows: list = []
        self._last_activity: Optional[ActivitySnapshot] = None
        self.bg = Background(root)

        self.queue: DeliveryQueue = open_queue(cfg)
        try:
            self.registry: BotRegistry = make_registry(cfg, cfg_path, self.queue)
            self.registry_error = ""
        except Exception as exc:
            from ..credentials import MemoryCredentialStore
            self.registry = BotRegistry(cfg, MemoryCredentialStore(), save=lambda: cfg.save(cfg_path), queue=self.queue)
            self.registry_error = sanitize(str(exc))
        self.factory = ClientFactory(cfg.telegram, self.registry.token_for)
        self.registry.listeners.append(lambda action, bot_id: self.factory.invalidate(bot_id))
        self._migration_notes = run_migrations(cfg, cfg_path, self.registry, self.queue)

        root.title(f"Monitor Screen — {SOURCE_LABEL}")
        try:
            root.geometry("1360x900")
            root.minsize(1100, 720)
        except tk.TclError:
            pass
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
        self.refresh_settings()
        self.root.after(200, self._pump_events)
        self.root.after(500, self._refresh_preview)
        self.root.after(1000, self._refresh_capture_panel)
        self.root.after(2000, self._refresh_diagnostics)
        if not any(b.enabled for b in self.registry.bots):
            self.show_page("bots")
            self.log_line("Setup: add at least one Telegram bot, then select the Studio window on the Monitor page "
                          "and start monitoring.")
        if autostart and self.cfg.target.is_set:
            self.root.after(1500, self.start)

    # ================================================================ layout
    def _build(self) -> None:
        self._build_header()
        body = tb.Frame(self.root)
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        self.content = tb.Frame(body, padding=(16, 12))
        self.content.pack(side="left", fill="both", expand=True)
        self.pages: dict[str, tb.Frame] = {key: tb.Frame(self.content) for key, _t, _i in PAGES}
        self._build_monitor_page(self.pages["monitor"])
        self._build_bots_page(self.pages["bots"])
        self._build_history_page(self.pages["history"])
        self._build_settings_page(self.pages["settings"])
        self._build_diagnostics_page(self.pages["diagnostics"])
        self._build_statusbar()
        self.show_page("monitor")

    def _build_header(self) -> None:
        hdr = tb.Frame(self.root, padding=(16, 10))
        hdr.pack(fill="x")
        brand = tb.Frame(hdr)
        brand.pack(side="left")
        tb.Label(brand, image=ico("broadcast", size=28, color="primary")).pack(side="left", padx=(0, 10))
        tt = tb.Frame(brand)
        tt.pack(side="left")
        tb.Label(tt, text="Monitor Screen", font=self.fonts["title"]).pack(anchor="w")
        tb.Label(tt, text=f"{SOURCE_LABEL} · v{__version__}", font=self.fonts["caption"], bootstyle="secondary").pack(anchor="w")

        owner = tb.Frame(hdr)
        owner.pack(side="left", padx=(36, 0))
        tb.Label(owner, text="Whose PC?", font=self.fonts["strong"]).grid(row=0, column=0, sticky="w")
        self.owner_var = tk.StringVar(value=self.cfg.owner_name)
        entry = tb.Entry(owner, textvariable=self.owner_var, width=22)
        entry.grid(row=1, column=0, sticky="w")
        entry.bind("<Return>", lambda e: self.save_owner())
        self.owner_save_btn = tb.Button(owner, text="Save", command=self.save_owner, bootstyle="primary-outline",
                                        image=ico("save2", color="primary"), compound="left")
        self.owner_save_btn.grid(row=1, column=1, padx=(6, 0))
        self.owner_label_var = tk.StringVar()
        tb.Label(owner, textvariable=self.owner_label_var, font=self.fonts["caption"], bootstyle="info").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ToolTip(entry, text=f"Example: Roy → notifications say “Roy’s Live”. Max {MAX_OWNER_NAME} characters; "
                            "blank uses the machine label.")
        self._show_owner_label()

        acct = tb.Frame(hdr)
        acct.pack(side="left", padx=(28, 0))
        tb.Label(acct, text="TikTok account", font=self.fonts["strong"]).grid(row=0, column=0, columnspan=2, sticky="w")
        self.account_var = tk.StringVar(value="Not detected")
        self.account_label = tb.Label(acct, textvariable=self.account_var, font=self.fonts["body"])
        self.account_label.grid(row=1, column=0, sticky="w")
        self.account_test_btn = tb.Button(acct, text="Detect now", command=self.test_account_lookup, bootstyle="secondary-outline",
                                          image=ico("person-badge"), compound="left")
        self.account_test_btn.grid(row=1, column=1, padx=(8, 0))
        ToolTip(self.account_test_btn, text="Opens Studio’s profile menu once to read the account username.")
        self.account_meta_var = tk.StringVar(value="")
        tb.Label(acct, textvariable=self.account_meta_var, font=self.fonts["caption"], bootstyle="secondary", wraplength=360,
                 justify="left").grid(row=2, column=0, columnspan=2, sticky="w")
        self.detect_var = tk.BooleanVar(value=self.cfg.account.detect_on_broadcast)
        chk = tb.Checkbutton(acct, text="Detect account when broadcast starts", variable=self.detect_var,
                             bootstyle="round-toggle", command=self._toggle_detect)
        chk.grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ToolTip(chk, text="Opens Studio’s profile menu once per broadcast to read the account username.")
        self._show_account(None)

        right = tb.Frame(hdr)
        right.pack(side="right")
        self.start_btn = tb.Button(right, text="Start monitoring", command=self.start, bootstyle="success",
                                   image=ico("play-fill", color="light"), compound="left")
        self.start_btn.pack(side="right")
        self.stop_btn = tb.Button(right, text="Stop", command=self.stop, bootstyle="danger-outline", state="disabled",
                                  image=ico("stop-fill", color="danger"), compound="left")
        self.stop_btn.pack(side="right", padx=8)
        pills = tb.Frame(hdr)
        pills.pack(side="right", padx=(0, 24))
        self.pill_monitor = Pill(pills, "STOPPED", "secondary")
        self.pill_capture = Pill(pills, "Capture: not started", "secondary")
        self.pill_live = Pill(pills, "Broadcast: unknown", "secondary")
        self.pill_stream = Pill(pills, "Stream: not evaluated", "secondary")
        for p in (self.pill_monitor, self.pill_capture, self.pill_live, self.pill_stream):
            p.pack(side="left", padx=4)
        ToolTip(self.pill_monitor, text="Monitoring status (RUNNING / DEGRADED / LOST / STOPPED)")
        ToolTip(self.pill_capture, text="Capture health of the selected Studio window")
        ToolTip(self.pill_live, text="Confirmed broadcast state from Studio UI evidence")
        ToolTip(self.pill_stream, text="Stream-health detectors (connection, source, preview, presenter, audio meter); evaluated only while LIVE")
        tb.Separator(self.root).pack(fill="x")

    def _build_sidebar(self, body) -> None:
        side = tb.Frame(body, padding=(8, 12), width=190)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.nav_var = tk.StringVar(value="monitor")
        self.nav_buttons: dict[str, tb.Radiobutton] = {}
        for key, title, icon in PAGES:
            btn = tb.Radiobutton(side, text=f"  {title}", value=key, variable=self.nav_var, bootstyle="toolbutton",
                                 image=ico(icon, size=18), compound="left", command=lambda k=key: self.show_page(k),
                                 padding=(10, 8), width=18)
            btn.pack(fill="x", pady=2)
            self.nav_buttons[key] = btn
        tb.Separator(body, orient="vertical").pack(side="left", fill="y")

    def _build_statusbar(self) -> None:
        tb.Separator(self.root).pack(fill="x")
        bar = tb.Frame(self.root, padding=(16, 4))
        bar.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        tb.Label(bar, textvariable=self.status_var, font=self.fonts["caption"], bootstyle="secondary").pack(side="left")
        self.queue_var = tk.StringVar(value="")
        tb.Label(bar, textvariable=self.queue_var, font=self.fonts["caption"], bootstyle="secondary").pack(side="right")

    def show_page(self, key: str) -> None:
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.nav_var.set(key)
        if key == "history":
            self.refresh_history()
        elif key == "bots":
            self.refresh_bots()
        elif key == "diagnostics":
            self._refresh_diagnostics(once=True)

    def open_settings(self) -> None:
        self.show_page("settings")

    # ---------------------------------------------------------------- monitor page
    def _build_monitor_page(self, page) -> None:
        top = tb.Panedwindow(page, orient="horizontal")
        top.pack(fill="both", expand=True)

        left = tb.Labelframe(top, text="  Studio window", padding=10)
        top.add(left, weight=1)
        cols = ("title", "process", "pid", "size")
        self.tree = tb.Treeview(left, columns=cols, show="headings", height=9, selectmode="browse", bootstyle="primary")
        for col, text, width in (("title", "Window title", 230), ("process", "Process", 140), ("pid", "PID", 60), ("size", "Size", 80)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("studio", foreground=self.style.colors.primary)
        self.tree.pack(fill="both", expand=True)
        row = tb.Frame(left)
        row.pack(fill="x", pady=(8, 0))
        self.refresh_btn = tb.Button(row, text="Refresh", command=self.refresh_windows, bootstyle="secondary-outline",
                                     image=ico("arrow-clockwise"), compound="left")
        self.refresh_btn.pack(side="left")
        self.use_btn = tb.Button(row, text="Use selected window", command=self.use_selected, bootstyle="primary",
                                 image=ico("crosshair", color="light"), compound="left")
        self.use_btn.pack(side="left", padx=8)
        ToolTip(self.use_btn, text="Bind capture, preview, OCR and screenshots to this window only")
        self.target_var = tk.StringVar(value="No target selected.")
        tb.Label(left, textvariable=self.target_var, wraplength=380, justify="left", font=self.fonts["caption"]).pack(
            fill="x", pady=(8, 0))

        right = tb.Labelframe(top, text="  Live preview and regions", padding=10)
        top.add(right, weight=2)
        self.canvas = tk.Canvas(right, bg=self.style.colors.inputbg, highlightthickness=0, cursor="crosshair", height=330)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        rrow = tb.Frame(right)
        rrow.pack(fill="x", pady=(8, 4))
        self.region_kind = tk.StringVar(value="detect")
        tb.Label(rrow, text="Draw:", font=self.fonts["strong"]).pack(side="left")
        for text, value, style in (("Popup detection", "detect", "warning"), ("Redaction (privacy)", "redact", "danger"),
                                   ("Live-status", "live", "success"), ("Profile control", "profile", "info"),
                                   ("Presenter", "face", "danger"), ("Audio meter", "audio", "secondary")):
            tb.Radiobutton(rrow, text=text, variable=self.region_kind, value=value, bootstyle=f"{style}-outline-toolbutton",
                           padding=(8, 3)).pack(side="left", padx=3)
        self.region_clear_btn = tb.Button(rrow, text="Clear all", command=self.clear_regions, bootstyle="secondary-link")
        self.region_clear_btn.pack(side="right")
        self.region_remove_btn = tb.Button(rrow, text="Remove selected", command=self.remove_region,
                                           bootstyle="secondary-outline", image=ico("eraser"), compound="left")
        self.region_remove_btn.pack(side="right", padx=4)
        self.region_list = tk.Listbox(right, height=3, font=self.fonts["mono"], bg=self.style.colors.inputbg,
                                      fg=self.style.colors.inputfg, highlightthickness=0, relief="flat")
        self.region_list.pack(fill="x")
        tb.Label(right, font=self.fonts["caption"], bootstyle="secondary", wraplength=720, justify="left",
                 text="Drag on the preview to add a region. No detection regions = scan the whole window; no live-status "
                      "regions = classify from the whole window (less reliable). Draw a small 'Profile control' box around "
                      "Studio’s top-right avatar to calibrate account detection. 'Presenter' = camera preview area for face/"
                      "motion/frozen checks; 'Audio meter' = Studio’s level meter. Dialogs are always scanned whole.").pack(anchor="w", pady=(4, 0))

        tiles = tb.Frame(page)
        tiles.pack(fill="x", pady=(12, 0))
        self.tile_capture = Tile(tiles, "Capture", self.fonts)
        self.tile_studio = Tile(tiles, "Studio", self.fonts)
        self.tile_live = Tile(tiles, "Broadcast", self.fonts)
        self.tile_reminder = Tile(tiles, "Go-live reminder", self.fonts)
        self.tile_delivery = Tile(tiles, "Telegram delivery", self.fonts)
        for t in (self.tile_capture, self.tile_studio, self.tile_live, self.tile_reminder, self.tile_delivery):
            t.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.tile_capture.set("Not started", "Select the Studio window to begin")

        logf = tb.Labelframe(page, text="  Activity log", padding=(10, 6))
        logf.pack(fill="both", expand=True, pady=(12, 0))
        self.log = ScrolledText(logf, height=7, font=self.fonts["mono"], wrap="word", auto_hide=True)
        self.log.pack(fill="both", expand=True)
        self.log.text.configure(state="disabled")

    # ---------------------------------------------------------------- bots page
    def _build_bots_page(self, page) -> None:
        head = tb.Frame(page)
        head.pack(fill="x")
        tb.Label(head, text="Telegram bots", font=self.fonts["subtitle"]).pack(side="left")
        self.bots_count_pill = Pill(head, f"Bots: 0 / {MAX_BOTS}", "primary")
        self.bots_count_pill.pack(side="left", padx=12)
        self.bots_limit_note = tk.StringVar(value="")
        tb.Label(head, textvariable=self.bots_limit_note, bootstyle="danger", font=self.fonts["caption"]).pack(side="left")
        tb.Label(page, text=TOKEN_HELP, wraplength=960, justify="left", bootstyle="secondary", font=self.fonts["caption"]).pack(
            anchor="w", pady=(6, 10))

        cols = ("name", "username", "chat", "enabled", "test", "delivery", "pending")
        self.bots_tree = tb.Treeview(page, columns=cols, show="headings", height=11, selectmode="browse", bootstyle="primary")
        for col, text, width in (("name", "Name", 150), ("username", "Telegram bot", 140), ("chat", "Destination", 170),
                                 ("enabled", "Status", 80), ("test", "Last test", 220), ("delivery", "Last delivery", 260),
                                 ("pending", "Pending", 60)):
            self.bots_tree.heading(col, text=text)
            self.bots_tree.column(col, width=width, anchor="w")
        self.bots_tree.pack(fill="both", expand=True)
        self.bots_tree.bind("<<TreeviewSelect>>", lambda e: self._update_bot_buttons())
        bar = tb.Frame(page)
        bar.pack(fill="x", pady=8)
        self.add_btn = tb.Button(bar, text="Add", command=self.add_bot, bootstyle="primary", image=ico("plus-lg", color="light"),
                                 compound="left")
        self.add_btn.pack(side="left")
        self.bot_btns: dict[str, tb.Button] = {}
        for text, cmd, icon, style in (("Edit", self.edit_bot, "pencil-square", "secondary-outline"),
                                       ("Remove", self.remove_bot, "trash3", "danger-outline"),
                                       ("Enable/Disable", self.toggle_bot, "toggle-on", "secondary-outline"),
                                       ("Validate Bot", self.validate_selected_bot, "patch-check", "info-outline"),
                                       ("Send Test", self.test_selected_bot, "send", "success-outline")):
            b = tb.Button(bar, text=text, command=cmd, state="disabled", bootstyle=style, image=ico(icon), compound="left")
            b.pack(side="left", padx=4)
            self.bot_btns[text] = b
        ToolTip(self.bot_btns["Validate Bot"], text="getMe only: checks the token, sends nothing")
        ToolTip(self.bot_btns["Send Test"], text="Sends an explicit test message with a synthetic image to this bot only")
        self.bots_refresh_btn = tb.Button(bar, text="Refresh", command=self.refresh_bots, bootstyle="secondary-link",
                                          image=ico("arrow-clockwise"), compound="left")
        self.bots_refresh_btn.pack(side="right")
        tb.Label(page, wraplength=960, justify="left", bootstyle="secondary", font=self.fonts["caption"],
                 text="Tokens are stored in the Windows Credential Manager, never in settings, logs or history. Disabling or "
                      "removing a bot cancels its pending deliveries; messages Telegram already accepted cannot be recalled. "
                      "Delivery is at-least-once: after an ambiguous timeout a retry may send a message twice.").pack(anchor="w")
        self.bots_status = tk.StringVar(value="")
        tb.Label(page, textvariable=self.bots_status, bootstyle="info", wraplength=960, justify="left").pack(anchor="w", pady=6)

    # ---------------------------------------------------------------- history page
    def _build_history_page(self, page) -> None:
        head = tb.Frame(page)
        head.pack(fill="x")
        tb.Label(head, text="History", font=self.fonts["subtitle"]).pack(side="left")
        self._history_kind = tk.StringVar(value="all")
        seg = tb.Frame(head)
        seg.pack(side="left", padx=16)
        for label, value in (("All", "all"), ("Restrictions", "incident"), ("Studio activity", "activity")):
            tb.Radiobutton(seg, text=label, variable=self._history_kind, value=value, bootstyle="outline-toolbutton",
                           command=self.refresh_history, padding=(10, 4)).pack(side="left")
        self.history_refresh_btn = tb.Button(head, text="Refresh", command=self.refresh_history, bootstyle="secondary-link",
                                             image=ico("arrow-clockwise"), compound="left")
        self.history_refresh_btn.pack(side="right")
        cols = ("time", "kind", "label", "owner", "delivery")
        self.history = tb.Treeview(page, columns=cols, show="headings", height=10, selectmode="browse", bootstyle="primary")
        for col, text, width in (("time", "Time", 120), ("kind", "Kind", 80), ("label", "Event", 260), ("owner", "Label", 150),
                                 ("delivery", "Delivery", 320)):
            self.history.heading(col, text=text)
            self.history.column(col, width=width, anchor="w")
        self.history.pack(fill="both", expand=True, pady=(8, 0))
        self.history.bind("<<TreeviewSelect>>", lambda e: self._show_event_details())
        tb.Label(page, text="Per-bot delivery for the selected event", font=self.fonts["strong"]).pack(anchor="w", pady=(10, 4))
        self.details = tb.Treeview(page, columns=("bot", "dest", "status", "attempts", "msg", "error"), show="headings", height=5,
                                   selectmode="browse", bootstyle="secondary")
        for col, text, width in (("bot", "Bot", 130), ("dest", "Destination", 130), ("status", "Status", 80), ("attempts", "Attempts", 70),
                                 ("msg", "Message id", 90), ("error", "Result", 420)):
            self.details.heading(col, text=text)
            self.details.column(col, width=width, anchor="w")
        self.details.pack(fill="both", expand=True)
        self.details.bind("<<TreeviewSelect>>", lambda e: self._update_retry_button())
        bar = tb.Frame(page)
        bar.pack(fill="x", pady=8)
        self.retry_btn = tb.Button(bar, text="Retry selected delivery", command=self.retry_selected, state="disabled",
                                   bootstyle="warning-outline", image=ico("arrow-repeat"), compound="left")
        self.retry_btn.pack(side="left")
        ToolTip(self.retry_btn, text="Re-queues only this bot's delivery; bots that already succeeded are never resent")
        self.evidence_btn = tb.Button(bar, text="Open screenshot", command=self.open_evidence, state="disabled",
                                      bootstyle="secondary-outline", image=ico("image"), compound="left")
        self.evidence_btn.pack(side="left", padx=8)

    # ---------------------------------------------------------------- settings page
    def _build_settings_page(self, page) -> None:
        head = tb.Frame(page)
        head.pack(fill="x")
        tb.Label(head, text="Settings", font=self.fonts["subtitle"]).pack(side="left")
        self.settings_save_btn = tb.Button(head, text="Save settings", command=self.save_settings, bootstyle="primary",
                                           image=ico("save2", color="light"), compound="left")
        self.settings_save_btn.pack(side="right")
        self.settings_revert_btn = tb.Button(head, text="Revert", command=self.refresh_settings, bootstyle="secondary-outline")
        self.settings_revert_btn.pack(side="right", padx=8)
        self.enroll_btn = tb.Button(head, text="Enroll with pairing code", command=self.enroll_hub, bootstyle="info-outline",
                                    image=ico("link-45deg"), compound="left")
        self.enroll_btn.pack(side="right", padx=8)
        self.settings_status = tk.StringVar(value="")
        tb.Label(page, textvariable=self.settings_status, bootstyle="info", font=self.fonts["caption"]).pack(anchor="w", pady=(2, 6))

        scroller = ScrolledFrame(page, auto_hide=True)
        scroller.pack(fill="both", expand=True)
        self.set_vars: dict[str, tk.Variable] = {}
        c = self.cfg
        self.settings_spec = [
            ("Appearance & identity", [
                ("ui_theme", "Theme", ("choice", list(THEMES)), lambda: next((k for k, v in THEMES.items() if v == c.ui.theme), "Dark"),
                 lambda v: setattr(c.ui, "theme", THEMES.get(v, "bootstrap-dark")), "Applies immediately."),
                ("machine_label", "Machine label", "str", lambda: c.machine_label, lambda v: setattr(c, "machine_label", v or hostname()),
                 "Shown as PC in notifications; also the fallback when no owner name is set."),
                ("account_label", "Account label (optional)", "str", lambda: c.account_label, lambda v: setattr(c, "account_label", v),
                 "Operator-entered text added to broadcast alerts. Not a verified TikTok identity."),
            ]),
            ("Capture & health", [
                ("backend", "Capture backend", ("choice", ["auto", "wgc", "printwindow"]), lambda: c.capture.backend,
                 lambda v: setattr(c.capture, "backend", v), "auto prefers Windows Graphics Capture (recommended)."),
                ("fallback", "Allow explicit desktop-crop fallback", "bool", lambda: c.capture.allow_desktop_fallback,
                 lambda v: setattr(c.capture, "allow_desktop_fallback", v), "Only when the window is verifiably visible at its rectangle."),
                ("max_age", "Max frame age for evidence (s)", "float", lambda: c.capture.max_frame_age_seconds,
                 lambda v: setattr(c.capture, "max_frame_age_seconds", max(5.0, v)), ""),
                ("refresh", "WGC heartbeat refresh (s)", "float", lambda: c.capture.refresh_interval_seconds,
                 lambda v: setattr(c.capture, "refresh_interval_seconds", max(3.0, v)), ""),
                ("degrade_after", "Health alert after degraded for (s)", "float", lambda: c.health.degrade_after_seconds,
                 lambda v: setattr(c.health, "degrade_after_seconds", max(1.0, v)), ""),
                ("recover_after", "Health recovery after stable for (s)", "float", lambda: c.health.recover_after_seconds,
                 lambda v: setattr(c.health, "recover_after_seconds", max(1.0, v)), ""),
            ]),
            ("Stream health (while LIVE)", [
                ("det_enabled", "Enable stream-health detectors", "bool", lambda: c.detectors.enabled,
                 lambda v: setattr(c.detectors, "enabled", v), "Connection, missing source, black/frozen preview, presenter, audio meter. Restart monitoring to apply."),
                ("det_presenter", "Presenter (face) monitoring", "bool", lambda: c.detectors.presenter_enabled,
                 lambda v: setattr(c.detectors, "presenter_enabled", v), "Needs a 'Presenter' region. Local CPU face detection only; no identity recognition."),
                ("det_expected", "Presenter expected on camera", "bool", lambda: c.detectors.presenter_expected,
                 lambda v: setattr(c.detectors, "presenter_expected", v), "Off = no 'presenter not visible' alerts (music/gameplay streams)."),
                ("det_face_absent", "Face absent for (s)", "float", lambda: c.detectors.face_absent_seconds,
                 lambda v: setattr(c.detectors, "face_absent_seconds", max(5.0, v)), ""),
                ("det_motion_low", "Very still face for (s)", "float", lambda: c.detectors.motion_low_seconds,
                 lambda v: setattr(c.detectors, "motion_low_seconds", max(10.0, v)), ""),
                ("det_frozen", "Frozen preview for (s)", "float", lambda: c.detectors.frozen_seconds,
                 lambda v: setattr(c.detectors, "frozen_seconds", max(5.0, v)), ""),
                ("det_black", "Black preview for (s)", "float", lambda: c.detectors.black_preview_seconds,
                 lambda v: setattr(c.detectors, "black_preview_seconds", max(5.0, v)), ""),
                ("det_conn", "Connection message for (s)", "float", lambda: c.detectors.connection_sustain_seconds,
                 lambda v: setattr(c.detectors, "connection_sustain_seconds", max(2.0, v)), ""),
                ("det_audio", "Audio meter silence detection", "bool", lambda: c.detectors.audio_enabled,
                 lambda v: setattr(c.detectors, "audio_enabled", v), "Needs an 'Audio meter' region. Reads Studio’s on-screen meter only."),
                ("det_audio_s", "Audio silent for (s)", "float", lambda: c.detectors.audio_silence_seconds,
                 lambda v: setattr(c.detectors, "audio_silence_seconds", max(5.0, v)), ""),
                ("det_profile", "Audio profile", ("choice", ["mixed", "mic-only", "music-only"]), lambda: c.detectors.audio_profile,
                 lambda v: setattr(c.detectors, "audio_profile", v), "Informational label for the operator; thresholds are not changed automatically."),
            ]),
            ("Popup detection", [
                ("poll", "Poll interval (s)", "float", lambda: c.detection.poll_interval_seconds,
                 lambda v: setattr(c.detection, "poll_interval_seconds", max(0.5, v)), ""),
                ("confirm", "Confirm polls", "int", lambda: c.detection.confirm_polls, lambda v: setattr(c.detection, "confirm_polls", max(1, v)), ""),
                ("cooldown", "Dedup cooldown (s)", "float", lambda: c.detection.dedup_cooldown_seconds,
                 lambda v: setattr(c.detection, "dedup_cooldown_seconds", v), ""),
                ("dialogs", "Also capture separate Studio dialogs", "bool", lambda: c.detection.include_dialogs,
                 lambda v: setattr(c.detection, "include_dialogs", v), ""),
                ("rules", "Popup rules file (blank = bundled)", "str", lambda: c.detection.rules_file, lambda v: setattr(c.detection, "rules_file", v), ""),
            ]),
            ("Studio activity", [
                ("notify_opened", "Notify when Studio opens", "bool", lambda: c.activity.notify_opened, lambda v: setattr(c.activity, "notify_opened", v), ""),
                ("notify_closed", "Notify when Studio closes", "bool", lambda: c.activity.notify_closed, lambda v: setattr(c.activity, "notify_closed", v), ""),
                ("notify_already", "Notify if already running at monitor start", "bool", lambda: c.activity.notify_already_running,
                 lambda v: setattr(c.activity, "notify_already_running", v), ""),
                ("reminders", "Go-live reminders", "bool", lambda: c.activity.reminders_enabled, lambda v: setattr(c.activity, "reminders_enabled", v), ""),
                ("threshold", "Offline threshold (minutes)", "float", lambda: c.activity.offline_threshold_minutes,
                 lambda v: setattr(c.activity, "offline_threshold_minutes", max(1.0, v)), ""),
                ("repeat", "Repeat reminders", "bool", lambda: c.activity.repeat_enabled, lambda v: setattr(c.activity, "repeat_enabled", v), ""),
                ("repeat_interval", "Repeat interval (minutes)", "float", lambda: c.activity.repeat_interval_minutes,
                 lambda v: setattr(c.activity, "repeat_interval_minutes", max(1.0, v)), ""),
                ("repeat_max", "Maximum repeats", "int", lambda: c.activity.repeat_max_count, lambda v: setattr(c.activity, "repeat_max_count", max(0, v)), ""),
                ("open_timeout", "Open screenshot timeout (s)", "float", lambda: c.activity.open_screenshot_timeout_seconds,
                 lambda v: setattr(c.activity, "open_screenshot_timeout_seconds", max(1.0, v)), ""),
                ("close_debounce", "Close debounce (s)", "float", lambda: c.activity.close_debounce_seconds,
                 lambda v: setattr(c.activity, "close_debounce_seconds", max(1.0, v)), ""),
                ("max_gap", "Max observation gap (s)", "float", lambda: c.activity.max_observation_gap_seconds,
                 lambda v: setattr(c.activity, "max_observation_gap_seconds", max(1.0, v)), ""),
                ("confirm_obs", "Confirm observations (live state)", "int", lambda: c.activity.confirm_observations,
                 lambda v: setattr(c.activity, "confirm_observations", max(1, v)), ""),
                ("live_rules", "Live-state rules file (blank = bundled)", "str", lambda: c.activity.live_rules_file,
                 lambda v: setattr(c.activity, "live_rules_file", v), ""),
            ]),
            ("Privacy & delivery", [
                ("send_shots", "Attach screenshots to Telegram alerts", "bool", lambda: c.privacy.send_screenshots,
                 lambda v: setattr(c.privacy, "send_screenshots", v), ""),
                ("store_text", "Store detected text in local history", "bool", lambda: c.privacy.store_detected_text,
                 lambda v: setattr(c.privacy, "store_detected_text", v), ""),
                ("retention", "Screenshot retention (days)", "int", lambda: c.privacy.screenshot_retention_days,
                 lambda v: setattr(c.privacy, "screenshot_retention_days", max(0, v)), ""),
                ("maxtext", "Max detected-text characters in alerts", "int", lambda: c.privacy.max_text_in_alert,
                 lambda v: setattr(c.privacy, "max_text_in_alert", max(20, v)), ""),
                ("dead_age", "Dead-letter pending deliveries after (hours)", "float", lambda: c.telegram.delivery_max_age_hours,
                 lambda v: setattr(c.telegram, "delivery_max_age_hours", max(1.0, v)), ""),
                ("concurrency", "Bots delivered in parallel", "int", lambda: c.telegram.delivery_concurrency,
                 lambda v: setattr(c.telegram, "delivery_concurrency", max(1, min(10, v))), ""),
            ]),
            ("Telegram commands & escalation", [
                ("cmd_enabled", "Answer Telegram commands (/status, /screenshot, /ack ...)", "bool", lambda: c.commands.enabled,
                 lambda v: setattr(c.commands, "enabled", v), "Standalone mode only; one bot is polled. Only the bot's configured chat may issue commands."),
                ("cmd_buttons", "Inline Ack / Snooze / Screenshot buttons on incident alerts", "bool", lambda: c.commands.buttons,
                 lambda v: setattr(c.commands, "buttons", v), ""),
                ("esc_enabled", "Escalation route enabled", "bool", lambda: c.escalation.enabled, lambda v: setattr(c.escalation, "enabled", v),
                 "Notify an extra chat once an incident stayed unacknowledged through N reminders."),
                ("esc_after", "Escalate after N reminders", "int", lambda: c.escalation.after_reminders,
                 lambda v: setattr(c.escalation, "after_reminders", max(1, v)), ""),
                ("esc_chat", "Escalation chat id", "str", lambda: c.escalation.chat_id, lambda v: setattr(c.escalation, "chat_id", v.strip()), ""),
                ("smtp_enabled", "E-mail backup when Telegram delivery fails", "bool", lambda: c.smtp.enabled, lambda v: setattr(c.smtp, "enabled", v),
                 "Urgent events only by default. Password: `studio-monitor smtp set-password`."),
                ("smtp_host", "SMTP host", "str", lambda: c.smtp.host, lambda v: setattr(c.smtp, "host", v.strip()), ""),
                ("smtp_port", "SMTP port", "int", lambda: c.smtp.port, lambda v: setattr(c.smtp, "port", max(1, v)), "587 with STARTTLS, 465 for implicit TLS"),
                ("smtp_user", "SMTP username", "str", lambda: c.smtp.username, lambda v: setattr(c.smtp, "username", v.strip()), ""),
                ("smtp_from", "From address", "str", lambda: c.smtp.from_addr, lambda v: setattr(c.smtp, "from_addr", v.strip()), ""),
                ("smtp_to", "To addresses (comma separated)", "str", lambda: ", ".join(c.smtp.to_addrs),
                 lambda v: setattr(c.smtp, "to_addrs", [a.strip() for a in v.split(",") if a.strip()]), ""),
                ("smtp_tls", "Use STARTTLS", "bool", lambda: c.smtp.starttls, lambda v: setattr(c.smtp, "starttls", v), "Off = implicit TLS (SMTPS)."),
            ]),
            ("Fleet hub", [
                ("hub_url", "Hub URL", "str", lambda: c.hub.url, lambda v: setattr(c.hub, "url", v.strip().rstrip("/")),
                 "Central server (https://...). Use 'Enroll with pairing code' after saving; the agent credential is stored in the Credential Manager."),
                ("hub_mode", "Delivery mode", ("choice", ["standalone", "managed"]), lambda: c.device.mode,
                 lambda v: setattr(c.device, "mode", v), "managed = the hub sends Telegram notifications; standalone = this PC sends them."),
                ("hub_hb", "Heartbeat interval (s)", "float", lambda: c.hub.heartbeat_seconds,
                 lambda v: setattr(c.hub, "heartbeat_seconds", max(5.0, v)), "Hub marks the device unreachable after 90 s without a heartbeat."),
                ("hub_evidence", "Upload redacted screenshots to the hub", "bool", lambda: c.hub.upload_evidence,
                 lambda v: setattr(c.hub, "upload_evidence", v), "Only already-redacted evidence; also subject to the privacy screenshot setting."),
            ]),
            ("Device & schedule", [
                ("device_name", "Device display name", "str", lambda: c.device.device_name, lambda v: setattr(c.device, "device_name", v),
                 "Shown in the fleet hub. The device id is a stable installation UUID."),
                ("expected_account", "Expected TikTok account (optional)", "str", lambda: c.device.expected_account,
                 lambda v: setattr(c.device, "expected_account", v.strip().lstrip("@")), "Warns when the verified observed account differs."),
                ("sched_enabled", "Streaming schedule enabled", "bool", lambda: c.schedule.enabled, lambda v: _sched_set(c, "enabled", v), ""),
                ("sched_tz", "Schedule timezone (IANA)", "str", lambda: c.schedule.timezone, lambda v: _sched_set(c, "timezone", v or "UTC"), "e.g. Africa/Nairobi"),
                ("sched_days", "Weekdays", "str", lambda: ",".join(c.schedule.weekdays),
                 lambda v: _sched_set(c, "weekdays", [d.strip().lower()[:3] for d in v.split(",") if d.strip()]), "mon,tue,...,sun"),
                ("sched_start", "Start (HH:MM)", "str", lambda: c.schedule.start, lambda v: _sched_set(c, "start", v or "20:00"), ""),
                ("sched_end", "End (HH:MM)", "str", lambda: c.schedule.end, lambda v: _sched_set(c, "end", v or "23:00"), "end before start = overnight"),
                ("sched_grace", "Missed-start grace (minutes)", "int", lambda: c.schedule.grace_minutes, lambda v: _sched_set(c, "grace_minutes", max(0, v)), ""),
            ]),
            ("Startup", [
                ("signin", "Start Monitor Screen when I sign in to Windows", "bool", lambda: c.activity.start_at_signin,
                 lambda v: setattr(c.activity, "start_at_signin", v),
                 "Per-user Run key; starts monitoring the saved target. Nothing is observed while the monitor is not running."),
            ]),
        ]
        for section, items in self.settings_spec:
            card = tb.Labelframe(scroller, text=f"  {section}", padding=(12, 8))
            card.pack(fill="x", pady=(0, 10), padx=(0, 12))
            for r, (key, label, kind, getter, setter, hint) in enumerate(items):
                if kind == "bool":
                    var = tk.BooleanVar(value=bool(getter()))
                    tb.Checkbutton(card, text=label, variable=var, bootstyle="round-toggle").grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
                else:
                    tb.Label(card, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 12))
                    var = tk.StringVar(value=str(getter()))
                    if isinstance(kind, tuple):
                        tb.Combobox(card, textvariable=var, values=kind[1], state="readonly", width=24).grid(row=r, column=1, sticky="w")
                    else:
                        tb.Entry(card, textvariable=var, width=36).grid(row=r, column=1, sticky="w")
                if hint:
                    tb.Label(card, text=hint, bootstyle="secondary", font=self.fonts["caption"], wraplength=420, justify="left").grid(
                        row=r, column=2, sticky="w", padx=(14, 0))
                self.set_vars[key] = var

    # ---------------------------------------------------------------- diagnostics page
    def _build_diagnostics_page(self, page) -> None:
        head = tb.Frame(page)
        head.pack(fill="x")
        tb.Label(head, text="Diagnostics", font=self.fonts["subtitle"]).pack(side="left")
        self.diag_copy_btn = tb.Button(head, text="Copy", command=self.copy_diagnostics, bootstyle="secondary-outline",
                                       image=ico("clipboard"), compound="left")
        self.diag_copy_btn.pack(side="right")
        self.open_data_btn = tb.Button(head, text="Open data folder", command=self.open_data_folder, bootstyle="secondary-outline",
                                       image=ico("folder2-open"), compound="left")
        self.open_data_btn.pack(side="right", padx=8)
        self.calib_live_btn = tb.Button(head, text="Calibrate live state", command=self.calibrate_live, bootstyle="info-outline",
                                        image=ico("record-circle"), compound="left")
        self.calib_live_btn.pack(side="right", padx=8)
        self.calib_btn = tb.Button(head, text="Calibrate popups", command=self.calibrate, bootstyle="info-outline",
                                   image=ico("shield-exclamation"), compound="left")
        self.calib_btn.pack(side="right")
        tb.Label(page, bootstyle="secondary", font=self.fonts["caption"], wraplength=960, justify="left",
                 text="Technical details of capture, identity and delivery. Calibration runs OCR on a real Studio screenshot "
                      "and reports which rules fire; results appear in the activity log.").pack(anchor="w", pady=(4, 8))
        self.diag = ScrolledText(page, font=self.fonts["mono"], wrap="none", auto_hide=True)
        self.diag.pack(fill="both", expand=True)
        self.diag.text.configure(state="disabled")

    # ================================================================ helpers
    def log_line(self, msg: str) -> None:
        self.log.text.configure(state="normal")
        self.log.text.insert("end", f"{time.strftime('%H:%M:%S')}  {sanitize(msg)}\n")
        self.log.text.see("end")
        self.log.text.configure(state="disabled")
        self.status_var.set(sanitize(msg)[:140])

    def save(self) -> None:
        try:
            self.cfg.save(self.cfg_path)
        except OSError as exc:
            self.log_line(f"could not save config: {exc}")

    def _show_owner_label(self) -> None:
        self.owner_label_var.set(f"Notifications: “{self.cfg.notification_label}”")

    def _toggle_detect(self) -> None:
        self.cfg.account.detect_on_broadcast = bool(self.detect_var.get())
        self.save()
        self.log_line("account detection on broadcast start " + ("enabled" if self.cfg.account.detect_on_broadcast else "disabled"))

    def _show_account(self, snap: Optional[dict]) -> None:
        if snap is None:
            from ..account import IdentityStore
            a = IdentityStore(self.queue).load()
            snap = {"status": a.status, "username": a.username, "display_name": a.display_name, "source": a.source,
                    "observed_utc": a.observed_utc, "error": a.error, "is_current": False, "in_progress": False}
        handle = f"@{snap['username']}" if snap.get("username") else ""
        if snap.get("in_progress"):
            self.account_var.set("Looking up…")
            self.account_label.configure(bootstyle="info")
        elif handle and snap.get("is_current"):
            self.account_var.set(handle)
            self.account_label.configure(bootstyle="success")
        elif handle:
            self.account_var.set(f"Last detected: {handle}")
            self.account_label.configure(bootstyle="secondary")
        else:
            self.account_var.set("Not detected")
            self.account_label.configure(bootstyle="default")
        meta = []
        if snap.get("observed_utc"):
            meta.append(f"detected {_local(snap['observed_utc'])}" + (f" via {snap['source']}" if snap.get("source") else ""))
        st = snap.get("status", "")
        if st == "FAILED":
            meta.append(f"lookup failed: {snap.get('error') or 'unknown reason'}")
        elif st == "IN_PROGRESS":
            meta.append("lookup in progress")
        elif st == "DISABLED":
            meta.append("detection disabled")
        if snap.get("display_name") and handle:
            meta.append(f"display name “{snap['display_name']}” (not an identity)")
        self.account_meta_var.set(" · ".join(meta) if meta else "Opens Studio’s profile menu once per broadcast to read the account username.")

    def test_account_lookup(self) -> None:
        """Explicit, user-initiated lookup (opens the profile menu once)."""
        if not self.cfg.target.is_set:
            messagebox.showinfo("TikTok account", f"Select your {SOURCE_LABEL} window first.")
            return
        if not messagebox.askyesno("Detect TikTok account",
                                   "This opens Studio’s profile menu once (Studio must be idle and in front) and reads "
                                   "the @username, then closes the menu. Continue?"):
            return
        if self.monitor is not None:
            if self.monitor.request_account_lookup():
                self.account_var.set("Looking up…")
            else:
                self.log_line("a lookup is already running")
            return
        from ..account import AccountIdentity, IdentityStore, LookupContext, Win32Interactor, perform_lookup
        from ..ocr import create_backend
        a = self.cfg.account
        store = IdentityStore(self.queue)

        def work():
            ocr = create_backend(self.cfg.detection.ocr_backend, self.cfg.detection.ocr_language, self.cfg.detection.ocr_upscale)
            ctx = LookupContext(system=self.system, interactor=Win32Interactor(self.system), identity=self.cfg.target,
                                ocr=lambda img: ocr.recognize(img).text,
                                fresh_frame=lambda: (f.image if (f := self.capture_service.frame(max_age=float("inf"))) else None),
                                profile_region=self.cfg.profile_region, offset_right=a.profile_offset_right,
                                offset_top=a.profile_offset_top, idle_required=a.idle_seconds, timeout=a.timeout_seconds,
                                allow_physical=a.allow_physical_click)
            return perform_lookup(ctx)

        def done(res, exc):
            if exc is not None:
                self.log_line(f"account lookup error: {exc}")
                return
            ident = store.load()
            ident.status = res.status if res.status == "SUCCEEDED" else "FAILED"
            ident.username, ident.display_name, ident.source, ident.error = res.username, res.display_name, res.source, res.error
            from datetime import timezone as _tz
            ident.observed_utc = datetime.now(_tz.utc).isoformat(timespec="seconds")
            ident.attempts += 1
            store.save(ident)
            self._show_account({"status": ident.status, "username": ident.username, "display_name": ident.display_name,
                                "source": ident.source, "observed_utc": ident.observed_utc, "error": ident.error,
                                "is_current": True, "in_progress": False})
            self.log_line("account lookup: " + (f"@{res.username} via {res.source}" if res.username else f"failed ({res.error})")
                          + "; steps: " + "; ".join(res.steps))
        self.account_var.set("Looking up…")
        self.bg.run(work, done)

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
        self.log_line(f"owner name saved; new notifications are labelled “{self.cfg.notification_label}” "
                      "(already queued notifications keep their original label)")

    # ---------------------------------------------------------------- status rendering
    def _set_status(self, status: Status, reason: str = "") -> None:
        self.pill_monitor.set(status.value, STATUS_STYLE[status])
        if reason:
            self.status_var.set(reason)

    def _show_capture(self, cs: CaptureStatus, health: str = "", reason: str = "") -> None:
        h = health or cs.health
        r = reason or cs.reason
        backend = BACKEND_NAMES.get(cs.backend, cs.backend or "-")
        if cs.last_valid_at:
            age = max(0.0, time.time() - cs.last_valid_at)
            frame = f"last frame {datetime.fromtimestamp(cs.last_valid_at):%H:%M:%S} ({age:.0f}s ago, #{cs.frames})"
        else:
            frame = "no frame yet"
        text = {"OK": "OK", "DEGRADED": "Degraded", "NONE": "Not started"}.get(h, h)
        self.tile_capture.set(text, backend if cs.backend else "", (r + ("\n" if r else "") + frame).strip(),
                              HEALTH_STYLE.get(h, "secondary"))
        self.pill_capture.set(f"Capture: {text}" + (f" — {r}" if r and h != "OK" else ""), HEALTH_STYLE.get(h, "secondary"))

    def _refresh_capture_panel(self) -> None:
        try:
            if self.monitor is None:
                self._show_capture(self.capture_service.status())
        except Exception as exc:  # pragma: no cover
            self.log_line(f"capture panel error: {exc}")
        self.root.after(1000, self._refresh_capture_panel)

    def _show_activity(self, s: ActivitySnapshot) -> None:
        self._last_activity = s
        self._show_capture(s.capture, s.health.capture, s.health.capture_reason)
        if s.account:
            self._show_account(s.account)
        self.tile_studio.set(s.app_state.replace("_", " ").title(), f"session {s.session_id}" if s.session_id else "",
                             s.last_event or "", "success" if s.app_state == "RUNNING" else "secondary")
        verified = "" if s.live_rules_verified else "rules unverified · "
        self.tile_live.set(s.live_state.replace("_", " "),
                           verified + (f"confirmed {_local(s.last_confirmed_utc)}" if s.last_confirmed_utc else "not confirmed"),
                           (s.last_transition or s.live_evidence or s.last_observation or "")[:120], LIVE_STYLE.get(s.live_state, "secondary"))
        self.pill_live.set(f"Broadcast: {s.live_state.replace('_', ' ').lower()}", LIVE_STYLE.get(s.live_state, "secondary"))
        self._show_stream(s.stream)
        if s.episode_id:
            off = format_duration(s.offline_seconds) + ("  (counting)" if s.accumulating else "  (paused)")
            if s.remaining_seconds is None:
                rem = "reminder sent" if s.reminders_sent else "reminders disabled"
            else:
                rem = f"reminder in {format_duration(s.remaining_seconds)}"
            self.tile_reminder.set(off, "confirmed offline time", rem,
                                   "warning" if s.remaining_seconds is not None and s.remaining_seconds < 600 else "")
        else:
            self.tile_reminder.set("-", "no offline episode", "")
        self._show_delivery(s.delivery)

    def _show_delivery(self, d: dict) -> None:
        c = (d or {}).get("counts", {})
        last = (d or {}).get("last")
        summary = f"{c.get('sent', 0)} sent · {c.get('pending', 0)} pending · {c.get('failed', 0) + c.get('dead', 0)} blocked"
        detail = ""
        style = "success" if not c.get("failed") and not c.get("dead") else "warning"
        if last:
            detail = f"last: {last['id']} → {last.get('bot', '?')} {last['status']}"
            if last.get("error"):
                detail += f" ({last['error'][:70]})"
        self.tile_delivery.set(summary, "Telegram outbox", detail, style)
        self.queue_var.set(f"Outbox: {summary}")

    # ---------------------------------------------------------------- history
    def refresh_history(self) -> None:
        try:
            self._history_items = self.queue.history(80, self._history_kind.get())
        except Exception as exc:
            self.log_line(f"history unavailable: {exc}")
            return
        self.history.delete(*self.history.get_children())
        for i, it in enumerate(self._history_items):
            ts = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
            self.history.insert("", "end", iid=str(i), values=(ts, it["kind"], it["label"], it.get("owner_label", ""), it["detail"]))
        self.details.delete(*self.details.get_children())
        self._detail_rows = []
        self._update_retry_button()
        self._show_delivery(self.queue.delivery_status())

    def _selected_event(self) -> Optional[dict]:
        sel = self.history.selection()
        return self._history_items[int(sel[0])] if sel else None

    def _show_event_details(self) -> None:
        self.details.delete(*self.details.get_children())
        self._detail_rows = []
        item = self._selected_event()
        if item is None:
            self._update_retry_button()
            return
        for d in self.queue.deliveries_for(item["id"]):
            dest = d.chat_id + (f"/{d.thread_id}" if d.thread_id else "")
            self.details.insert("", "end", iid=str(d.id), values=(d.bot_name, dest, d.status, d.attempts, d.message_id or "-", d.last_error[:160]))
            self._detail_rows.append(d)
        self._update_retry_button()

    def _update_retry_button(self) -> None:
        sel = self.details.selection()
        ok = False
        if sel:
            d = next((x for x in self._detail_rows if str(x.id) == sel[0]), None)
            ok = d is not None and d.status in ("failed", "dead", "cancelled")
        self.retry_btn.configure(state="normal" if ok else "disabled")
        item = self._selected_event()
        has_shot = bool(item and item.get("evidence_path") and Path(item["evidence_path"]).exists())
        self.evidence_btn.configure(state="normal" if has_shot else "disabled")

    def retry_selected(self) -> None:
        sel = self.details.selection()
        if not sel:
            return
        if self.queue.retry_delivery(int(sel[0])):
            self.log_line(f"delivery {sel[0]} re-queued (only this bot; successful bots are not resent)")
            if self.monitor is not None and getattr(self.monitor, "worker", None):
                self.monitor.worker.kick()
            else:
                self._deliver_pending_once()
        self._show_event_details()

    def open_evidence(self) -> None:
        item = self._selected_event()
        if item and item.get("evidence_path") and Path(item["evidence_path"]).exists():
            self._open_path(item["evidence_path"])

    def _open_path(self, path: str) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            else:  # pragma: no cover
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            self.log_line(f"could not open {path}: {exc}")

    def _deliver_pending_once(self) -> None:
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

    # ---------------------------------------------------------------- bots
    def refresh_bots(self) -> None:
        reg = self.registry
        self.bots_count_pill.set(f"Bots: {reg.count} / {MAX_BOTS}", "primary" if reg.can_add else "warning")
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
            self.log_line(f"bot '{dlg.result.name}' added → {dlg.result.destination}")
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
                                                 f"delivery(ies) will be cancelled and its stored token deleted. History is kept."):
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
        try:
            self.bots_tree.selection_set(bot.bot_id)
            self._update_bot_buttons()
        except tk.TclError:
            pass

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

    # ---------------------------------------------------------------- settings
    def refresh_settings(self) -> None:
        for _section, items in self.settings_spec:
            for key, _label, kind, getter, _setter, _hint in items:
                var = self.set_vars[key]
                if kind == "bool":
                    var.set(bool(getter()))
                else:
                    var.set(str(getter()))
        self.settings_status.set("")

    def save_settings(self) -> None:
        before_signin = self.cfg.activity.start_at_signin
        before_theme = self.cfg.ui.theme
        pending = []
        for _section, items in self.settings_spec:
            for key, label, kind, _getter, setter, _hint in items:
                raw = self.set_vars[key].get()
                try:
                    if kind == "bool":
                        value = bool(raw)
                    elif kind == "int":
                        value = int(str(raw).strip())
                    elif kind == "float":
                        value = float(str(raw).strip())
                    else:
                        value = str(raw).strip()
                except ValueError:
                    messagebox.showerror("Settings", f"“{label}” must be a number.")
                    return
                pending.append((setter, value))
        for setter, value in pending:
            setter(value)
        self.save()
        if self.cfg.ui.theme != before_theme:
            try:
                self.style.theme_use(self.cfg.ui.theme)
                self.canvas.configure(bg=self.style.colors.inputbg)
                self.region_list.configure(bg=self.style.colors.inputbg, fg=self.style.colors.inputfg)
                self.tree.tag_configure("studio", foreground=self.style.colors.primary)
            except Exception as exc:
                self.log_line(f"theme change failed: {exc}")
        if self.cfg.activity.start_at_signin != before_signin:
            try:
                from ..startup import apply_setting
                apply_setting(self.cfg.activity.start_at_signin)
                self.log_line("start at sign-in " + ("enabled" if self.cfg.activity.start_at_signin else "disabled"))
            except Exception as exc:
                self.log_line(f"could not update sign-in startup setting: {exc}")
        self.refresh_settings()
        self.settings_status.set("Settings saved." + (" Restart monitoring to apply capture/detection changes." if self.monitor else ""))
        self.log_line("settings saved")

    # ---------------------------------------------------------------- diagnostics
    def _diagnostics_text(self) -> str:
        t = self.cfg.target
        cs = self.monitor.frames.status() if self.monitor is not None else self.capture_service.status()
        a = self._last_activity
        lines = [
            f"Monitor Screen v{__version__}   python {sys.version.split()[0]}   theme {self.cfg.ui.theme}",
            f"hostname={hostname()}  machine_label={self.cfg.machine_label}  owner={self.cfg.owner_name or '(blank)'}  "
            f"label={self.cfg.notification_label}",
            f"config={self.cfg_path}",
            f"data={self.cfg.data_path}",
            "",
            "[target]",
            (f'title="{t.title}" exe={t.exe_name} path={t.exe_path} pid={t.pid} start={t.process_start:.0f} hwnd=0x{t.hwnd:X} '
             f'class={t.class_name}') if t.is_set else "none selected",
            "",
            "[capture]",
            f"health={cs.health} code={cs.code or '-'} reason={cs.reason or '-'}",
            f"backend={cs.backend or '-'} hwnd=0x{cs.hwnd:X} frames={cs.frames} session_restarts={cs.session_restarts}",
            f"last_valid={datetime.fromtimestamp(cs.last_valid_at).strftime('%H:%M:%S') if cs.last_valid_at else '-'} "
            f"heartbeat={'alive' if cs.heartbeat_mono and time.monotonic() - cs.heartbeat_mono < 5 else 'stalled'}",
        ] + [f"{k}={v}" for k, v in (cs.diagnostics or {}).items()]
        if a is not None:
            h = a.health
            lines += ["", "[health]",
                      f"session={h.session} capture={h.capture} ({h.capture_reason or '-'}) ocr={h.ocr} "
                      f"broadcast={h.broadcast} delivery={h.delivery} ({h.delivery_reason or '-'})",
                      "", "[stream health]"] + ([f"{k}={v.get('state')} {v.get('detail') or ''}".rstrip() for k, v in a.stream.items()] or ["not evaluated (not LIVE)"]) + [
                      "", "[broadcast]", f"state={a.live_state} evidence={a.live_evidence or '-'}",
                      f"last_observation={a.last_observation or '-'}",
                      f"episode={a.broadcast_episode or '-'} transition={a.last_transition or '-'}",
                      "", "[reminder]",
                      f"episode={a.episode_id or '-'} offline={a.offline_seconds:.0f}s accumulating={a.accumulating} "
                      f"remaining={a.remaining_seconds} sent={a.reminders_sent}"]
        if a is not None and a.hub:
            hb = a.hub
            lines += ["", "[hub]", f"url={self.cfg.hub.url} connected={hb.get('connected')} pending={hb.get('pending')} "
                      f"evidence_pending={hb.get('evidence_pending')} rejected={hb.get('rejected')} uploaded={hb.get('uploaded_total')} "
                      f"last_error={hb.get('last_error') or '-'}"]
        elif self.cfg.hub.url:
            lines += ["", "[hub]", f"url={self.cfg.hub.url} enrolled={self.cfg.hub.enrolled} (sync starts with monitoring)"]
        counts = self.queue.counts()
        lines += ["", "[outbox]", " ".join(f"{k}={v}" for k, v in counts.items()),
                  f"bots={self.registry.count} enabled={sum(1 for b in self.registry.bots if b.enabled)} "
                  f"credential_store={type(self.registry.store).__name__}"]
        return "\n".join(lines)

    def _refresh_diagnostics(self, once: bool = False) -> None:
        try:
            if self.nav_var.get() == "diagnostics":
                text = self._diagnostics_text()
                self.diag.text.configure(state="normal")
                self.diag.text.delete("1.0", "end")
                self.diag.text.insert("end", text)
                self.diag.text.configure(state="disabled")
        except Exception as exc:  # pragma: no cover
            self.log_line(f"diagnostics error: {exc}")
        if not once:
            self.root.after(2000, self._refresh_diagnostics)

    def copy_diagnostics(self) -> None:
        text = self._diagnostics_text()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log_line("diagnostics copied to the clipboard")

    def open_data_folder(self) -> None:
        self._open_path(str(self.cfg.data_path))

    # ================================================================ target & preview
    def refresh_windows(self) -> None:
        self.windows = selectable_windows(self.system)
        self.tree.delete(*self.tree.get_children())
        for w in self.windows:
            tags = ("studio",) if looks_like_studio(w) else ()
            self.tree.insert("", "end", iid=str(w.hwnd), values=(w.title, w.exe_name, w.pid, f"{w.rect.width}x{w.rect.height}"), tags=tags)

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

    def _refresh_preview(self) -> None:
        try:
            cap = self.capture_service.frame(max_age=float("inf"))
            if cap is not None and cap.hwnd == self.cfg.target.hwnd:
                self.preview_image = cap.image
                self._draw_preview()
        except Exception as exc:  # never kill the UI loop
            self.log_line(f"preview error: {exc}")
        self.root.after(700, self._refresh_preview)

    def enroll_hub(self) -> None:
        """Pair this PC with the fleet hub. The pairing code is single use; the secret never touches settings."""
        dlg = EnrollDialog(self.root, self.cfg.hub.url, self.cfg.device.mode)
        if not dlg.result:
            return
        url, code, mode = dlg.result
        try:
            res = enroll_agent(self.cfg, self.cfg_path, url, code, mode)
        except Exception as exc:
            messagebox.showerror("Enrollment failed", sanitize(str(exc)))
            self.log_line(f"hub enrollment failed: {sanitize(str(exc))}")
            return
        self.refresh_settings()
        self.log_line(f"enrolled with hub {url} as device {res['device_id']} ({res['mode']} mode); restart monitoring to start syncing")
        messagebox.showinfo("Enrolled", f"This PC is now enrolled in workspace {res['workspace_id'] or 'default'} "
                                        f"({res['mode']} mode). Stop and start monitoring to begin heartbeats.")

    def _show_stream(self, stream: dict) -> None:
        if not stream:
            self.pill_stream.set("Stream: not evaluated", "secondary")
            return
        problems = [k.replace("_", " ").lower() for k, v in stream.items() if v.get("state") == "PROBLEM"]
        evaluated = sum(1 for v in stream.values() if v.get("state") in ("OK", "PROBLEM"))
        if problems:
            self.pill_stream.set("Stream: " + ", ".join(problems), "warning")
        elif evaluated:
            self.pill_stream.set(f"Stream: OK ({evaluated} checks)", "success")
        else:
            self.pill_stream.set("Stream: unknown (no fresh frame)", "secondary")

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
            self.canvas.create_text(l + 4, t + 4, anchor="nw", text=r.name, fill=color, font=self.fonts["caption"])

    def _drag_begin(self, event) -> None:
        if self.preview_image is None:
            return
        self._drag_start = (event.x, event.y)
        self._drag_rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#3dd5f3", width=2, dash=(4, 2))

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
        try:
            region = Region.from_pixels(f"{kind} {n}", box, self.preview_image.size, kind)
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
        if self.cfg.regions and not messagebox.askyesno("Clear regions", "Remove all detection, redaction and live-status regions?"):
            return
        self.cfg.regions.clear()
        self.save()
        self._refresh_region_list()
        self._draw_preview()

    # ================================================================ monitoring
    def start(self) -> None:
        if self.monitor is not None:
            return
        if not self.cfg.target.is_set:
            messagebox.showinfo("Start", f"Select your {SOURCE_LABEL} window first.")
            self.show_page("monitor")
            return
        if not any(b.enabled for b in self.registry.bots):
            if not messagebox.askyesno("No Telegram bots", "No enabled Telegram bot is configured; events will be recorded "
                                                           "with no deliveries. Start anyway?"):
                return
        try:
            setup_logging(self.cfg)
            self.monitor = build_monitor(
                self.cfg, self.cfg_path, registry=self.registry, queue=self.queue,
                on_event=lambda m: self.events.put(("event", m)),
                on_status=lambda s: self.events.put(("status", s)),
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
        self.status_var.set("Monitoring started.")

    def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._set_status(Status.STOPPED, "Monitoring stopped (nothing is observed while stopped).")
        self.tile_studio.set("Not observing", "monitor stopped", "", "secondary")
        if self.cfg.target.is_set and self.cfg.target.hwnd:
            self.capture_service.bind(self.cfg.target.hwnd)   # keep the preview of the selected window alive

    def _pump_events(self) -> None:
        refresh_hist = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "event":
                    self.log_line(payload)
                    if any(w in payload for w in ("queued", "cancelled", "delivered", "failed")):
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
            if self.nav_var.get() == "history":
                self.refresh_history()
            else:
                self._show_delivery(self.queue.delivery_status())
            if self.nav_var.get() == "bots":
                self.refresh_bots()
        self.root.after(200, self._pump_events)

    # ================================================================ calibration
    def _pick_image(self, title: str) -> str:
        return filedialog.askopenfilename(title=title, filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])

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
        self.show_page("monitor")

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
        self.show_page("monitor")

    # ================================================================ lifecycle
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


def run_gui(cfg: AppConfig, cfg_path: Path, autostart: bool = False) -> int:
    root = tb.Window(theme=cfg.ui.theme, title="Monitor Screen")
    App(root, cfg, cfg_path, autostart=autostart)
    root.mainloop()
    return 0
