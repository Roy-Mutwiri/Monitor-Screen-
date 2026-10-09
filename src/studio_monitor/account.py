"""Automatic TikTok account (@username) discovery when a broadcast starts.

Flow (bounded to ``AccountConfig.timeout_seconds``):

1. Try Windows UI Automation on the validated Studio window (no focus
   change). Studio's Chromium accessibility tree was found empty on the
   reference machine, so this normally fails fast.
2. Otherwise a *guarded physical interaction*: wait for user inactivity,
   revalidate the window identity (hwnd + pid + process creation time +
   executable), require the Studio window to be foreground (bring it forward
   at most once) and the click point to hit-test to Studio, click the
   calibrated profile control, read the popup menu that Studio opens as a
   separate window (OCR, cross-checked over two frames), then close only
   that popup (Escape while Studio is foreground, otherwise toggle) and
   restore the previous foreground window if the user did not switch.

Only an explicit ``@handle`` counts as a username. A display name without a
handle is reported as "username unavailable"; nothing is inferred from chat,
overlays or arbitrary screen text. Identity is stored per broadcast episode
with its source and observation time.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from PIL import Image

from .config import TargetIdentity
from .regions import Region
from .target import validate_handle
from .win32.windows import Rect, WindowInfo, WindowSystem

log = logging.getLogger(__name__)

STATE_KEY = "account_identity"
NOT_ATTEMPTED, IN_PROGRESS, SUCCEEDED, FAILED = "NOT_ATTEMPTED", "IN_PROGRESS", "SUCCEEDED", "FAILED"
DISABLED_STATUS = "DISABLED"

HANDLE_RE = re.compile(r"(?<![A-Za-z0-9._])@([A-Za-z0-9][A-Za-z0-9._]{1,23})(?![A-Za-z0-9._])")
DEFAULT_OFFSET_RIGHT = 190   # px from the right edge of the Studio window to the avatar centre (reference machine)
DEFAULT_OFFSET_TOP = 24


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- identity record

@dataclass
class AccountIdentity:
    episode_id: str = ""
    session_id: str = ""
    status: str = NOT_ATTEMPTED
    username: str = ""           # verified @handle (without the @)
    display_name: str = ""       # kept separate; never used as identity
    source: str = ""             # uia | popup-ocr | frame-ocr
    observed_utc: str = ""
    attempts: int = 0
    error: str = ""

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else ""

    def account_line(self) -> str:
        """Text for the 'TikTok account:' notification line."""
        if self.status == SUCCEEDED and self.username:
            return f"@{self.username}"
        if self.status == "DISABLED":
            return "not detected (automatic detection disabled)"
        return "unavailable — automatic lookup failed"


class IdentityStore:
    def __init__(self, queue) -> None:
        self.queue = queue

    def load(self) -> AccountIdentity:
        raw = self.queue.get_state(STATE_KEY) or {}
        return AccountIdentity(**{k: v for k, v in raw.items() if k in AccountIdentity.__dataclass_fields__})

    def save(self, ident: AccountIdentity) -> None:
        self.queue.set_state(STATE_KEY, asdict(ident))


# ---------------------------------------------------------------- username extraction

def extract_username(texts: list[str]) -> tuple[Optional[str], Optional[str], str]:
    """Return (username, display_name, reason).

    ``texts`` are OCR/accessibility readings of the opened profile menu, one
    per frame. The handle must appear as an explicit ``@name``; readings that
    disagree are rejected as ambiguous. The display name is the first
    non-handle line near the handle (informational only).
    """
    handles: list[str] = []
    display = None
    for text in texts:
        found = HANDLE_RE.findall(text or "")
        if not found:
            continue
        # a menu normally shows one handle; several distinct ones are ambiguous
        distinct = {h.rstrip(".") for h in found}
        if len(distinct) > 1:
            return None, None, f"ambiguous: several handles read ({', '.join(sorted(distinct))})"
        handle = distinct.pop()
        handles.append(handle)
        if display is None:
            for line in (text or "").splitlines():
                line = line.strip()
                if line and "@" not in line and 2 <= len(line) <= 40 and not re.search(r"(log ?out|switch|account|settings)", line, re.I):
                    display = line
                    break
    if not handles:
        joined = " ".join(t.strip() for t in texts if t and t.strip())
        if joined:
            for text in texts:
                for line in (text or "").splitlines():
                    line = line.strip()
                    if line and "@" not in line and 2 <= len(line) <= 40 and not re.search(r"(log ?out|switch|account|settings)", line, re.I):
                        display = display or line
                        break
            return None, display, "no @handle visible in the profile menu (display name only or menu not read)"
        return None, None, "profile menu text could not be read"
    if len(set(handles)) > 1:
        return None, display, f"ambiguous: frames disagree ({', '.join(sorted(set(handles)))})"
    return handles[0], display, ""


# ---------------------------------------------------------------- interaction abstraction

class Interactor(Protocol):
    """Everything the lookup needs from the OS; faked in tests."""

    def foreground(self) -> int: ...
    def bring_to_front(self, hwnd: int) -> bool: ...
    def idle_seconds(self) -> float: ...
    def hit_test(self, x: int, y: int) -> int: ...
    def click(self, x: int, y: int) -> None: ...
    def escape(self) -> None: ...
    def studio_windows(self, main: WindowInfo) -> dict[int, WindowInfo]: ...
    def capture_window(self, win: WindowInfo) -> Optional[Image.Image]: ...
    def uia_read(self, hwnd: int) -> Optional[list[str]]: ...


class Win32Interactor:
    """Real implementation (SendInput, SetForegroundWindow, PrintWindow)."""

    def __init__(self, system: WindowSystem) -> None:
        self.system = system

    def foreground(self) -> int:
        return self.system.foreground_window()

    def bring_to_front(self, hwnd: int) -> bool:
        from .win32 import api
        import ctypes
        # A brief Alt tap releases the foreground lock so SetForegroundWindow may succeed once.
        self._key(0x12); self._key(0x12, up=True)
        api.user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        return self.system.foreground_window() == hwnd

    def idle_seconds(self) -> float:
        import ctypes
        from ctypes import wintypes
        from .win32 import api

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not api.user32.GetLastInputInfo(ctypes.byref(li)):
            return 0.0
        return max(0.0, (api.kernel32.GetTickCount() - li.dwTime) / 1000.0)

    def hit_test(self, x: int, y: int) -> int:
        return self.system.window_at_point(x, y)

    def _send(self, inp) -> None:
        import ctypes
        from .win32 import api
        api.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def _key(self, vk: int, up: bool = False) -> None:
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]
        inp = INPUT(type=1)
        inp.u.ki = KEYBDINPUT(vk, 0, 2 if up else 0, 0, None)
        self._send(inp)

    def click(self, x: int, y: int) -> None:
        import ctypes
        from ctypes import wintypes
        from .win32 import api

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]
        screen = self.system.virtual_screen()
        ax = int((x - screen.left) * 65535 / max(1, screen.width - 1))
        ay = int((y - screen.top) * 65535 / max(1, screen.height - 1))
        for flags in (0x0001 | 0x8000 | 0x4000, 0x0002, 0x0004):   # move (absolute, virtual desk), down, up
            inp = INPUT(type=0)
            inp.u.mi = MOUSEINPUT(ax, ay, 0, flags, 0, None)
            self._send(inp)
            time.sleep(0.03)

    def escape(self) -> None:
        self._key(0x1B); self._key(0x1B, up=True)

    def studio_windows(self, main: WindowInfo) -> dict[int, WindowInfo]:
        return {w.hwnd: w for w in self.system.list_windows(include_invisible=True)
                if w.visible and (w.pid == main.pid or w.exe_name.lower() == main.exe_name.lower())}

    def capture_window(self, win: WindowInfo) -> Optional[Image.Image]:
        from .win32.capture import print_window, screen_crop, is_blank
        img = print_window(win.hwnd)
        if img is None and self.system.window_at_point(win.rect.left + 4, win.rect.top + 4) == win.hwnd:
            img = screen_crop(win.rect)
            if img is not None and is_blank(img):
                img = None
        return img

    def uia_read(self, hwnd: int) -> Optional[list[str]]:
        """Names of accessible descendants (None when the tree is unavailable)."""
        try:
            import uiautomation as auto
        except Exception:
            return None
        try:
            root = auto.ControlFromHandle(hwnd)
            texts = []

            def walk(c, depth=0):
                if depth > 12 or len(texts) > 800:
                    return
                for k in c.GetChildren():
                    if k.Name:
                        texts.append(k.Name)
                    walk(k, depth + 1)
            walk(root)
            return texts or None
        except Exception as exc:  # pragma: no cover
            log.debug("uia read failed: %s", exc)
            return None


# ---------------------------------------------------------------- lookup

@dataclass
class LookupContext:
    system: WindowSystem
    interactor: Interactor
    identity: TargetIdentity
    ocr: Callable[[Image.Image], str]
    fresh_frame: Callable[[], Optional[Image.Image]]
    profile_region: Optional[Region] = None       # calibrated, relative to the window frame
    offset_right: int = DEFAULT_OFFSET_RIGHT
    offset_top: int = DEFAULT_OFFSET_TOP
    idle_required: float = 1.5
    timeout: float = 10.0
    blocked: Callable[[], bool] = lambda: False    # restriction / verification dialog present
    allow_physical: bool = True
    clock: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = lambda m: None


@dataclass
class LookupResult:
    status: str
    username: str = ""
    display_name: str = ""
    source: str = ""
    error: str = ""
    attempts: int = 0
    observed_at: float = 0.0
    opened_menu: bool = False
    closed_menu: bool = True
    focus_changed: bool = False
    focus_restored: bool = False
    steps: list[str] = field(default_factory=list)


def click_point(window: WindowInfo, region: Optional[Region], offset_right: int, offset_top: int) -> tuple[int, int]:
    """Screen coordinates (physical pixels; the process is per-monitor DPI
    aware) of the profile control: centre of the calibrated region, else the
    default right-anchored offset."""
    r = window.rect
    if region is not None:
        left, top, right, bottom = region.to_box(r.width, r.height)
        return r.left + (left + right) // 2, r.top + (top + bottom) // 2
    return r.right - offset_right, r.top + offset_top


def perform_lookup(ctx: LookupContext) -> LookupResult:
    """Run the discovery synchronously (call from a worker thread)."""
    res = LookupResult(status=FAILED)
    deadline = ctx.mono() + ctx.timeout
    steps = res.steps

    def time_left() -> float:
        return deadline - ctx.mono()

    def revalidate() -> Optional[WindowInfo]:
        v = validate_handle(ctx.system, ctx.identity)
        return v.window if v.ok else None

    win = revalidate()
    if win is None:
        res.error = "target window invalid before lookup"
        return res

    # 1) accessibility first: no focus change, no clicks
    try:
        names = ctx.interactor.uia_read(win.hwnd)
    except Exception:
        names = None
    if names:
        user, display, why = extract_username(["\n".join(names)])
        if user:
            res.status, res.username, res.display_name, res.source = SUCCEEDED, user, display or "", "uia"
            res.observed_at = ctx.clock()
            res.attempts = 1
            steps.append("uia: handle read from accessibility tree")
            return res
        steps.append(f"uia: tree readable but {why}")
    else:
        steps.append("uia: accessibility tree unavailable")

    if not ctx.allow_physical:
        res.error = "accessibility unavailable and physical interaction disabled"
        return res

    prev_fg = ctx.interactor.foreground()
    for attempt in (1, 2):
        res.attempts = attempt
        # 2) wait for the user to be idle and for no blocking dialog
        while time_left() > 1.0 and (ctx.interactor.idle_seconds() < ctx.idle_required or ctx.blocked()):
            ctx.sleep(0.25)
        if time_left() <= 1.0:
            res.error = "deferred: user active or a Studio dialog is present until the lookup timeout"
            return res
        win = revalidate()
        if win is None:
            res.error = "target window exited during lookup"
            return res
        if win.minimized or win.cloaked or not win.visible:
            res.error = "Studio window is minimized or hidden"
            return res
        x, y = click_point(win, ctx.profile_region, ctx.offset_right, ctx.offset_top)
        if not (win.rect.left <= x < win.rect.right and win.rect.top <= y < win.rect.bottom):
            res.error = "profile control point is outside the Studio window (recalibrate)"
            return res
        # 3) foreground + hit test
        if ctx.interactor.foreground() != win.hwnd:
            if res.focus_changed or not ctx.interactor.bring_to_front(win.hwnd):
                res.error = "could not bring Studio to the foreground (not retried)"
                return res
            res.focus_changed = True
            steps.append("brought Studio to the foreground once")
        hit = ctx.interactor.hit_test(x, y)
        if hit != win.hwnd:
            res.error = f"profile control point is covered by another window (hwnd 0x{hit:X})"
            return res
        # 4) click and read the popup menu
        before = set(ctx.interactor.studio_windows(win))
        ctx.interactor.click(x, y)
        res.opened_menu = True
        res.closed_menu = False
        steps.append(f"clicked profile control at ({x},{y})")
        texts: list[str] = []
        popups: list[WindowInfo] = []
        for _ in range(6):
            ctx.sleep(0.25)
            now_wins = ctx.interactor.studio_windows(win)
            popups = [w for h, w in now_wins.items() if h not in before and h != win.hwnd]
            if popups:
                break
        for _frame in range(2):
            for p in popups:
                img = ctx.interactor.capture_window(p)
                if img is not None:
                    texts.append(ctx.ocr(img))
            if not popups:
                img = ctx.fresh_frame()
                if img is not None:
                    w_, h_ = img.size
                    texts.append(ctx.ocr(img.crop((int(w_ * 0.55), 0, w_, int(h_ * 0.6)))))
            ctx.sleep(0.2)
        user, display, why = extract_username(texts)
        # 5) close only what we opened
        still_fg = ctx.interactor.foreground() in {win.hwnd, *[p.hwnd for p in popups]}
        if still_fg:
            ctx.interactor.escape()
            ctx.sleep(0.4)
        remaining = [h for h in ctx.interactor.studio_windows(win) if h not in before and h != win.hwnd]
        if remaining and still_fg and ctx.interactor.hit_test(x, y) == win.hwnd:
            ctx.interactor.click(x, y)     # toggle the menu closed
            ctx.sleep(0.4)
            remaining = [h for h in ctx.interactor.studio_windows(win) if h not in before and h != win.hwnd]
        res.closed_menu = not remaining
        steps.append("menu closed" if res.closed_menu else "menu may still be open")
        if user:
            res.status, res.username, res.display_name = SUCCEEDED, user, display or ""
            res.source = "popup-ocr" if popups else "frame-ocr"
            res.observed_at = ctx.clock()
            break
        res.error = why
        transient = not popups and time_left() > 3.0
        if not transient or attempt == 2:
            break
        steps.append("no menu appeared; one bounded retry")
    # 6) restore focus only if we changed it and the user has not switched since
    if res.focus_changed and ctx.interactor.foreground() == win.hwnd and prev_fg and prev_fg != win.hwnd:
        res.focus_restored = ctx.interactor.bring_to_front(prev_fg)
    return res


class AccountLookupJob:
    """Runs :func:`perform_lookup` on a worker thread with a hard deadline."""

    def __init__(self, ctx: LookupContext, episode_id: str, started_mono: float) -> None:
        self.ctx = ctx
        self.episode_id = episode_id
        self.started_mono = started_mono
        self.result: Optional[LookupResult] = None
        self._thread = threading.Thread(target=self._run, name="account-lookup", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            self.result = perform_lookup(self.ctx)
        except Exception as exc:  # pragma: no cover
            log.exception("account lookup crashed")
            self.result = LookupResult(status=FAILED, error=f"lookup error: {exc}")

    @property
    def done(self) -> bool:
        return self.result is not None

    def expired(self, now_mono: float) -> bool:
        return now_mono - self.started_mono >= self.ctx.timeout
