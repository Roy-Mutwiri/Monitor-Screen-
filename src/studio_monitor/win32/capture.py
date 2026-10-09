"""Window capture bound to one validated HWND.

Backends (``FrameSource``), in order of preference:

* ``wgc``          Windows Graphics Capture (``windows-capture`` binding, Win10
                   1903+). Captures the window's own composited surface even
                   when another application covers it. Delivers a frame only
                   when the content changes, so the service restarts the
                   session periodically as a heartbeat (a fresh session always
                   yields one frame).
* ``printwindow``  ``PrintWindow(PW_RENDERFULLCONTENT)``. Works for many
                   windows; GPU-composited (Chromium/Electron) windows return
                   an empty bitmap while occluded, which is why it is only
                   trusted when the frame is non-blank.
* ``desktop-crop`` Explicit fallback: crops the screen at the window
                   rectangle. Only used when conservative checks show the
                   window is the one actually visible at its own rectangle
                   (foreground, on-screen, not covered at sample points).

:class:`CaptureService` runs the chosen backend on its own thread, keeps a
bounded queue of the newest frames and reports capture health with a reason.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from PIL import Image

from . import api
from .windows import Rect, WindowInfo, WindowSystem

log = logging.getLogger(__name__)

BACKEND_WGC = "wgc"
BACKEND_PRINTWINDOW = "printwindow"
BACKEND_DESKTOP = "desktop-crop"


@dataclass
class Capture:
    image: Image.Image
    window: WindowInfo
    method: str  # backend name
    reliable: bool
    note: str = ""
    is_dialog: bool = False
    captured_at: float = 0.0     # wall clock
    captured_mono: float = 0.0   # monotonic
    hwnd: int = 0
    seq: int = 0


class Capturer(Protocol):
    """Pull-style capture used for dialogs and the synchronous test path."""

    def capture(self, window: WindowInfo, foreground_hwnd: int = 0) -> Optional[Capture]: ...


def is_blank(image: Image.Image) -> bool:
    """True when the bitmap is uniform (a native call 'succeeded' but painted
    nothing). Static but real content is *not* blank."""
    small = image.convert("L").resize((48, 48))
    lo, hi = small.getextrema()
    return hi - lo < 4


# ---------------------------------------------------------------- GDI helpers

def _bitmap_to_image(hdc, bmp, w: int, h: int) -> Optional[Image.Image]:
    bmi = api.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(api.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = api.BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    lines = api.gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bmi), api.DIB_RGB_COLORS)
    if lines != h:
        return None
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()


def print_window(hwnd: int) -> Optional[Image.Image]:
    """PrintWindow(PW_RENDERFULLCONTENT) of a top-level window; None on failure
    or when the window painted nothing (blank)."""
    rect = ctypes.wintypes.RECT()
    if not api.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hdc_win = api.user32.GetWindowDC(hwnd)
    if not hdc_win:
        return None
    hdc_mem = bmp = old = None
    try:
        hdc_mem = api.gdi32.CreateCompatibleDC(hdc_win)
        bmp = api.gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        if not hdc_mem or not bmp:
            return None
        old = api.gdi32.SelectObject(hdc_mem, bmp)
        if not api.user32.PrintWindow(hwnd, hdc_mem, api.PW_RENDERFULLCONTENT):
            return None
        img = _bitmap_to_image(hdc_mem, bmp, w, h)
        if img is None or is_blank(img):
            return None
        return img
    finally:
        if old:
            api.gdi32.SelectObject(hdc_mem, old)
        if bmp:
            api.gdi32.DeleteObject(bmp)
        if hdc_mem:
            api.gdi32.DeleteDC(hdc_mem)
        api.user32.ReleaseDC(hwnd, hdc_win)


def screen_crop(r: Rect) -> Optional[Image.Image]:
    if r.width <= 0 or r.height <= 0:
        return None
    hdc_screen = api.user32.GetDC(0)
    if not hdc_screen:
        return None
    hdc_mem = bmp = old = None
    try:
        hdc_mem = api.gdi32.CreateCompatibleDC(hdc_screen)
        bmp = api.gdi32.CreateCompatibleBitmap(hdc_screen, r.width, r.height)
        if not hdc_mem or not bmp:
            return None
        old = api.gdi32.SelectObject(hdc_mem, bmp)
        if not api.gdi32.BitBlt(hdc_mem, 0, 0, r.width, r.height, hdc_screen, r.left, r.top, api.SRCCOPY):
            return None
        return _bitmap_to_image(hdc_mem, bmp, r.width, r.height)
    finally:
        if old:
            api.gdi32.SelectObject(hdc_mem, old)
        if bmp:
            api.gdi32.DeleteObject(bmp)
        if hdc_mem:
            api.gdi32.DeleteDC(hdc_mem)
        api.user32.ReleaseDC(0, hdc_screen)


def window_visible_at_rect(system: WindowSystem, window: WindowInfo) -> tuple[bool, str]:
    """Conservative check that the window is what the screen shows at its own
    rectangle: foreground, on-screen, and the top-level window at several
    sample points is the window itself."""
    if system.foreground_window() != window.hwnd:
        return False, "another window is in the foreground"
    r = window.rect
    screen = system.virtual_screen()
    if r.left < screen.left or r.top < screen.top or r.right > screen.right or r.bottom > screen.bottom:
        return False, "window extends off-screen"
    points = [(r.left + r.width // 2, r.top + r.height // 2), (r.left + 8, r.top + 8), (r.right - 8, r.top + 8),
              (r.left + 8, r.bottom - 8), (r.right - 8, r.bottom - 8)]
    for x, y in points:
        if system.window_at_point(x, y) != window.hwnd:
            return False, "another window covers the target"
    return True, ""


# ---------------------------------------------------------------- pull capturer (dialogs, tests)

class Win32Capturer:
    """PrintWindow for a window (dialogs). No desktop fallback: an empty
    PrintWindow result yields None rather than someone else's pixels."""

    def __init__(self, allow_screen_fallback: bool = False, system: Optional[WindowSystem] = None) -> None:
        self.allow_screen_fallback = allow_screen_fallback
        self.system = system

    def capture(self, window: WindowInfo, foreground_hwnd: int = 0) -> Optional[Capture]:
        if window.minimized or window.cloaked:
            return None
        now, mono = time.time(), time.monotonic()
        img = print_window(window.hwnd)
        if img is not None:
            return Capture(img, window, BACKEND_PRINTWINDOW, True, captured_at=now, captured_mono=mono, hwnd=window.hwnd)
        if not self.allow_screen_fallback or self.system is None:
            return None
        ok, why = window_visible_at_rect(self.system, window)
        if not ok:
            return None
        img = screen_crop(window.rect)
        if img is None or is_blank(img):
            return None
        return Capture(img, window, BACKEND_DESKTOP, True, "desktop crop (window verified visible)",
                       captured_at=now, captured_mono=mono, hwnd=window.hwnd)


# ---------------------------------------------------------------- WGC backend

class WgcSession:
    """One Windows Graphics Capture session for one HWND (windows-capture)."""

    def __init__(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture  # noqa: F401  (import error -> unavailable)
        self.hwnd = hwnd
        self._lock = threading.Lock()
        self._latest: Optional[tuple[Image.Image, float, float]] = None   # image, wall, mono
        self.frames = 0
        self.closed = False
        self.error: str = ""
        self._control = None
        self._capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

        @self._capture.event
        def on_frame_arrived(frame, control):
            try:
                buf = frame.frame_buffer
                h, w = buf.shape[0], buf.shape[1]
                img = Image.frombuffer("RGBA", (w, h), buf.tobytes(), "raw", "BGRA", 0, 1).convert("RGB")
            except Exception as exc:  # pragma: no cover
                self.error = f"frame conversion failed: {exc}"
                return
            with self._lock:
                self._latest = (img, time.time(), time.monotonic())
                self.frames += 1

        @self._capture.event
        def on_closed():
            self.closed = True

    @staticmethod
    def available() -> bool:
        try:
            import windows_capture  # noqa: F401
            import numpy  # noqa: F401
            return True
        except Exception:
            return False

    def start(self) -> None:
        self._control = self._capture.start_free_threaded()

    @property
    def alive(self) -> bool:
        if self.closed or self._control is None:
            return False
        try:
            return not self._control.is_finished()
        except Exception:
            return False

    def latest(self) -> Optional[tuple[Image.Image, float, float]]:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self.closed = True
        try:
            if self._control is not None:
                self._control.stop()
        except Exception:
            pass
        self._control = None


# ---------------------------------------------------------------- capture service

@dataclass
class CaptureStatus:
    health: str = "NONE"             # OK | DEGRADED | NONE
    reason: str = ""                 # friendly reason when not OK
    code: str = ""                   # stable reason code
    backend: str = ""
    hwnd: int = 0
    last_valid_at: float = 0.0       # wall clock of the newest valid frame
    last_valid_mono: float = 0.0
    heartbeat_mono: float = 0.0      # worker loop liveness, independent of pixel changes
    frames: int = 0
    session_restarts: int = 0
    diagnostics: dict = field(default_factory=dict)


REASONS = {
    "no_target": "No Studio window selected",
    "target_closed": "Target window closed",
    "minimized": "Studio is minimized (window capture pauses while minimized)",
    "hidden": "Studio window is hidden or cloaked",
    "desktop_locked": "Desktop locked (nothing can be captured until unlock)",
    "wgc_init_failed": "Window capture initialization failed",
    "device_lost": "Graphics capture device lost; recovering",
    "blank": "Window produced empty frames",
    "stale": "No fresh frame from the window",
    "fallback_unavailable": "Desktop fallback cannot see the target reliably",
    "fallback": "Using explicit desktop fallback (window capture unavailable)",
}


class FrameService(Protocol):
    """What the monitor consumes."""

    def bind(self, hwnd: int) -> None: ...
    def frame(self) -> Optional[Capture]: ...
    def status(self) -> CaptureStatus: ...
    def stop(self) -> None: ...


class SyncFrameService:
    """Pull-based service around a :class:`Capturer` (tests and fallback)."""

    def __init__(self, system: WindowSystem, capturer: Capturer, max_age: float = 30.0,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.system = system
        self.capturer = capturer
        self.max_age = max_age
        self.clock = clock
        self.mono = mono
        self.hwnd = 0
        self._status = CaptureStatus(backend=BACKEND_PRINTWINDOW)
        self._seq = 0

    def bind(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._status.hwnd = hwnd

    def frame(self) -> Optional[Capture]:
        st = self._status
        st.heartbeat_mono = self.mono()
        if not self.hwnd:
            st.health, st.code, st.reason = "NONE", "no_target", REASONS["no_target"]
            return None
        win = self.system.get_window(self.hwnd)
        if win is None:
            st.health, st.code, st.reason = "DEGRADED", "target_closed", REASONS["target_closed"]
            return None
        if win.minimized:
            st.health, st.code, st.reason = "DEGRADED", "minimized", REASONS["minimized"]
            return None
        if win.cloaked or not win.visible:
            st.health, st.code, st.reason = "DEGRADED", "hidden", REASONS["hidden"]
            return None
        cap = self.capturer.capture(win, self.system.foreground_window())
        if cap is None or is_blank(cap.image):
            st.health, st.code, st.reason = "DEGRADED", "blank", REASONS["blank"]
            return None
        self._seq += 1
        cap.captured_at, cap.captured_mono, cap.hwnd, cap.seq = self.clock(), self.mono(), self.hwnd, self._seq
        cap.reliable = True
        st.health, st.code, st.reason = "OK", "", ""
        st.backend = cap.method
        st.last_valid_at, st.last_valid_mono, st.frames = cap.captured_at, cap.captured_mono, self._seq
        if cap.method == BACKEND_DESKTOP:
            st.health, st.code, st.reason = "OK", "fallback", REASONS["fallback"]
        return cap

    def status(self) -> CaptureStatus:
        return self._status

    def stop(self) -> None:
        self.hwnd = 0


class CaptureService:
    """Threaded capture bound to one HWND. Preference: WGC -> PrintWindow ->
    explicit desktop crop (visibility-verified). Keeps the newest frames in a
    bounded queue and recreates the WGC session on size change, device loss or
    as a periodic heartbeat."""

    def __init__(self, system: WindowSystem, *, interval: float = 0.5, max_age: float = 30.0,
                 refresh_interval: float = 15.0, allow_desktop_fallback: bool = True,
                 prefer: str = "auto", queue_size: int = 2,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.system = system
        self.interval = interval
        self.max_age = max_age
        self.refresh_interval = refresh_interval
        self.allow_desktop_fallback = allow_desktop_fallback
        self.prefer = prefer
        self.clock = clock
        self.mono = mono
        self._frames: deque = deque(maxlen=queue_size)
        self._lock = threading.RLock()
        self._status = CaptureStatus()
        self._hwnd = 0
        self._session: Optional[WgcSession] = None
        self._session_size: tuple[int, int] = (0, 0)
        self._session_started_mono = 0.0
        self._last_session_frames = 0
        self._wgc_failures = 0
        self._wgc_disabled_until = 0.0
        self._seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.wgc_available = (prefer in ("auto", BACKEND_WGC)) and WgcSession.available()

    # -- lifecycle ------------------------------------------------------
    def bind(self, hwnd: int) -> None:
        with self._lock:
            if hwnd != self._hwnd:
                self._teardown_session()
                self._frames.clear()
                self._hwnd = hwnd
                self._status = CaptureStatus(hwnd=hwnd)
                self._wgc_failures = 0
                self._wgc_disabled_until = 0.0
        if hwnd and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="capture-service", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=3)
        with self._lock:
            self._teardown_session()
            self._hwnd = 0
            self._frames.clear()
            self._status = CaptureStatus()

    def _teardown_session(self) -> None:
        if self._session is not None:
            try:
                self._session.stop()
            finally:
                self._session = None

    # -- consumers --------------------------------------------------------
    def frame(self, max_age: Optional[float] = None) -> Optional[Capture]:
        """Newest valid frame if it is fresh (age <= max_age), else None.
        ``max_age=float('inf')`` returns the newest frame regardless (preview)."""
        with self._lock:
            if not self._frames:
                return None
            cap = self._frames[-1]
        limit = self.max_age if max_age is None else max_age
        if self.mono() - cap.captured_mono > limit:
            return None
        return cap

    def status(self) -> CaptureStatus:
        with self._lock:
            st = CaptureStatus(**{k: v for k, v in self._status.__dict__.items()})
            st.diagnostics = dict(self._status.diagnostics)
        return st

    # -- worker -----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            started = self.mono()
            try:
                self._tick()
            except Exception as exc:  # keep the worker alive
                log.exception("capture tick failed")
                self._set("DEGRADED", "wgc_init_failed", extra=str(exc))
            elapsed = self.mono() - started
            self._stop.wait(max(0.05, self.interval - elapsed))
        with self._lock:
            self._teardown_session()

    def _set(self, health: str, code: str, backend: Optional[str] = None, extra: str = "") -> None:
        with self._lock:
            st = self._status
            st.health, st.code = health, code
            st.reason = (REASONS.get(code, code) + (f" ({extra})" if extra else "")) if code else ""
            if backend is not None:
                st.backend = backend
            st.heartbeat_mono = self.mono()
            st.diagnostics.update({"wgc_available": self.wgc_available, "wgc_failures": self._wgc_failures,
                                   "session_alive": bool(self._session and self._session.alive),
                                   "session_frames": self._session.frames if self._session else 0})

    def _push(self, image: Image.Image, win: WindowInfo, backend: str, note: str = "") -> None:
        now, mono = self.clock(), self.mono()
        with self._lock:
            self._seq += 1
            cap = Capture(image, win, backend, True, note, captured_at=now, captured_mono=mono, hwnd=win.hwnd,
                          seq=self._seq)
            self._frames.append(cap)
            st = self._status
            st.last_valid_at, st.last_valid_mono, st.frames, st.backend = now, mono, self._seq, backend

    def _tick(self) -> None:
        hwnd = self._hwnd
        if not hwnd:
            self._set("NONE", "no_target")
            return
        win = self.system.get_window(hwnd)
        if win is None:
            self._teardown_session()
            self._set("DEGRADED", "target_closed")
            return
        if self.system.desktop_locked():
            self._set("DEGRADED", "desktop_locked")
            return
        if win.minimized:
            self._set("DEGRADED", "minimized")
            return
        if win.cloaked or not win.visible:
            self._set("DEGRADED", "hidden")
            return

        if self.wgc_available and self.mono() >= self._wgc_disabled_until:
            if self._wgc_tick(win):
                return
        if self.prefer in ("auto", BACKEND_PRINTWINDOW, BACKEND_WGC):
            img = print_window(win.hwnd)
            if img is not None:
                self._push(img, win, BACKEND_PRINTWINDOW)
                self._set("OK", "", BACKEND_PRINTWINDOW)
                return
        if self.allow_desktop_fallback:
            ok, why = window_visible_at_rect(self.system, win)
            if ok:
                img = screen_crop(win.rect)
                if img is not None and not is_blank(img):
                    self._push(img, win, BACKEND_DESKTOP, "desktop crop; window verified visible at its rectangle")
                    self._set("OK", "fallback", BACKEND_DESKTOP)
                    return
            self._set("DEGRADED", "fallback_unavailable", extra=why)
            return
        self._set("DEGRADED", "blank")

    def _wgc_tick(self, win: WindowInfo) -> bool:
        """Serve a frame from WGC. Returns True when WGC is in charge (even if
        the current frame is simply unchanged)."""
        size = win.rect.size
        now = self.mono()
        sess = self._session
        need_new = (sess is None or not sess.alive or sess.closed
                    or (self._session_size != size)
                    or (now - self._session_started_mono >= self.refresh_interval
                        and sess.frames == self._last_session_frames))
        if sess is not None and not sess.alive and sess.error:
            self._set("DEGRADED", "device_lost", extra=sess.error)
        if need_new:
            restart = sess is not None
            self._teardown_session()
            try:
                sess = WgcSession(win.hwnd)
                sess.start()
            except Exception as exc:
                self._wgc_failures += 1
                self._wgc_disabled_until = now + min(60.0, 2.0 * self._wgc_failures)
                self._set("DEGRADED", "wgc_init_failed", extra=str(exc)[:120])
                return False
            self._session, self._session_size, self._session_started_mono = sess, size, now
            self._last_session_frames = 0
            if restart:
                with self._lock:
                    self._status.session_restarts += 1
            # a new session delivers its first frame quickly; wait briefly for it
            deadline = now + 1.5
            while self.mono() < deadline and sess.latest() is None and sess.alive:
                time.sleep(0.03)
        latest = sess.latest()
        if latest is None:
            if not sess.alive:
                self._set("DEGRADED", "device_lost", BACKEND_WGC)
                return True
            if self.mono() - self._session_started_mono > 3.0:
                self._set("DEGRADED", "stale", BACKEND_WGC)
            return True
        img, wall, mono = latest
        if sess.frames != self._last_session_frames:
            self._last_session_frames = sess.frames
            if is_blank(img):
                self._set("DEGRADED", "blank", BACKEND_WGC)
                return True
            self._push(img, win, BACKEND_WGC)
        self._wgc_failures = 0
        self._set("OK", "", BACKEND_WGC)
        return True
