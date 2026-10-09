"""Window capture.

Primary path: ``PrintWindow(PW_RENDERFULLCONTENT)`` which asks the window to
render itself into our bitmap. This works for Chromium/Electron windows (which
TikTok LIVE Studio is) and does not require the window to be on top.

Fallback: a screen grab of the window's rectangle. That only sees what is on
the screen, so the tracker marks the capture as *unreliable* when it is used
while another window is in the foreground.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Optional, Protocol

from PIL import Image

from . import api
from .windows import Rect, WindowInfo


@dataclass
class Capture:
    image: Image.Image
    window: WindowInfo
    method: str  # "printwindow" | "screen"
    reliable: bool
    note: str = ""
    is_dialog: bool = False  # True for a separate Studio dialog/window, False for the main window


class Capturer(Protocol):
    def capture(self, window: WindowInfo, foreground_hwnd: int = 0) -> Optional[Capture]: ...


def _is_blank(image: Image.Image) -> bool:
    """True when the bitmap is uniformly black/white (PrintWindow silently failed)."""
    small = image.convert("L").resize((32, 32))
    lo, hi = small.getextrema()
    return hi - lo < 4


class Win32Capturer:
    def __init__(self, allow_screen_fallback: bool = True) -> None:
        self.allow_screen_fallback = allow_screen_fallback

    def capture(self, window: WindowInfo, foreground_hwnd: int = 0) -> Optional[Capture]:
        if window.minimized or window.cloaked:
            return None
        img = self._print_window(window.hwnd)
        if img is not None and not _is_blank(img):
            return Capture(img, window, "printwindow", True)
        if not self.allow_screen_fallback:
            return None
        img = self._screen_grab(window.rect)
        if img is None or _is_blank(img):
            return None
        reliable = foreground_hwnd in (0, window.hwnd)
        note = "" if reliable else "screen-grab fallback; another window is in the foreground"
        return Capture(img, window, "screen", reliable, note)

    # -- PrintWindow --------------------------------------------------------
    def _print_window(self, hwnd: int) -> Optional[Image.Image]:
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
            return _bitmap_to_image(hdc_mem, bmp, w, h)
        finally:
            if old:
                api.gdi32.SelectObject(hdc_mem, old)
            if bmp:
                api.gdi32.DeleteObject(bmp)
            if hdc_mem:
                api.gdi32.DeleteDC(hdc_mem)
            api.user32.ReleaseDC(hwnd, hdc_win)

    # -- screen grab --------------------------------------------------------
    def _screen_grab(self, r: Rect) -> Optional[Image.Image]:
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
