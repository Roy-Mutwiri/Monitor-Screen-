"""Window enumeration, process identity discovery and window relationships.

The :class:`WindowSystem` protocol is what the rest of the monitor depends on,
so tests can substitute a fake. :class:`Win32WindowSystem` is the real thing.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional, Protocol

from . import api


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe_path: str
    rect: Rect
    visible: bool
    minimized: bool
    cloaked: bool
    owner_hwnd: int
    tool_window: bool

    @property
    def exe_name(self) -> str:
        return os.path.basename(self.exe_path) if self.exe_path else ""

    def describe(self) -> str:
        return f'"{self.title}" [{self.exe_name or "?"} pid={self.pid} hwnd=0x{self.hwnd:X}]'


class WindowSystem(Protocol):
    """Everything the monitor needs to know about windows and processes."""

    def list_windows(self, include_invisible: bool = False) -> list[WindowInfo]: ...
    def get_window(self, hwnd: int) -> Optional[WindowInfo]: ...
    def is_window(self, hwnd: int) -> bool: ...
    def process_alive(self, pid: int) -> bool: ...
    def process_exe_path(self, pid: int) -> str: ...
    def process_start_time(self, pid: int) -> float: ...
    def process_tree(self, root_pid: int) -> set[int]: ...
    def foreground_window(self) -> int: ...
    def virtual_screen(self) -> Rect: ...
    def window_at_point(self, x: int, y: int) -> int: ...
    def desktop_locked(self) -> bool: ...


class Win32WindowSystem:
    """Real Win32 implementation (ctypes)."""

    def __init__(self) -> None:
        if not api.AVAILABLE:  # pragma: no cover
            raise RuntimeError("Win32WindowSystem requires Windows")
        self._exe_cache: dict[int, str] = {}

    # -- processes --------------------------------------------------------
    def process_exe_path(self, pid: int) -> str:
        cached = self._exe_cache.get(pid)
        if cached is not None:
            return cached
        path = ""
        handle = api.kernel32.OpenProcess(api.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if api.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    path = buf.value
            finally:
                api.kernel32.CloseHandle(handle)
        if path:
            self._exe_cache[pid] = path
        return path

    def process_start_time(self, pid: int) -> float:
        """Process creation time (epoch seconds); 0.0 if unreadable. Combined
        with the pid and executable it defeats pid reuse."""
        handle = api.kernel32.OpenProcess(api.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return 0.0
        try:
            c, e, k, u = api.FILETIME(), api.FILETIME(), api.FILETIME(), api.FILETIME()
            if api.kernel32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return c.to_epoch()
            return 0.0
        finally:
            api.kernel32.CloseHandle(handle)

    def process_alive(self, pid: int) -> bool:
        return pid in self._snapshot_processes()

    def window_at_point(self, x: int, y: int) -> int:
        """Top-level window visible at a screen point (0 if none)."""
        hwnd = api.user32.WindowFromPoint(api.POINT(x, y))
        if not hwnd:
            return 0
        root = api.user32.GetAncestor(hwnd, api.GA_ROOT)
        return int(root or hwnd)

    def desktop_locked(self) -> bool:
        """True when the interactive desktop is not the input desktop (lock
        screen / secure desktop), in which case nothing can be captured."""
        handle = api.user32.OpenInputDesktop(0, False, api.DESKTOP_READOBJECTS)
        if not handle:
            return True
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD(0)
            api.user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            if api.user32.GetUserObjectInformationW(handle, 2, buf, 512, ctypes.byref(needed)):   # UOI_NAME
                return buf.value.lower() != "default"
            return False
        finally:
            api.user32.CloseDesktop(handle)

    def _snapshot_processes(self) -> dict[int, tuple[int, str]]:
        """pid -> (parent pid, exe name)."""
        result: dict[int, tuple[int, str]] = {}
        snap = api.kernel32.CreateToolhelp32Snapshot(api.TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return result
        try:
            entry = api.PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(api.PROCESSENTRY32W)
            ok = api.kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                result[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
                ok = api.kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            api.kernel32.CloseHandle(snap)
        return result

    def process_tree(self, root_pid: int) -> set[int]:
        """``root_pid`` plus every live descendant (Electron spawns helpers)."""
        procs = self._snapshot_processes()
        tree = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, (ppid, _name) in procs.items():
                if ppid in tree and pid not in tree:
                    tree.add(pid)
                    changed = True
        # Drop pids that no longer exist (root included).
        return {pid for pid in tree if pid in procs}

    # -- windows ----------------------------------------------------------
    def is_window(self, hwnd: int) -> bool:
        return bool(api.user32.IsWindow(hwnd))

    def foreground_window(self) -> int:
        return int(api.user32.GetForegroundWindow() or 0)

    def virtual_screen(self) -> Rect:
        x = api.user32.GetSystemMetrics(api.SM_XVIRTUALSCREEN)
        y = api.user32.GetSystemMetrics(api.SM_YVIRTUALSCREEN)
        w = api.user32.GetSystemMetrics(api.SM_CXVIRTUALSCREEN)
        h = api.user32.GetSystemMetrics(api.SM_CYVIRTUALSCREEN)
        return Rect(x, y, x + w, y + h)

    def _title(self, hwnd: int) -> str:
        length = api.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        api.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _class_name(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        api.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _rect(self, hwnd: int) -> Rect:
        rect = wintypes.RECT()
        # Extended frame bounds exclude the invisible resize borders Windows 10/11
        # add around top-level windows; fall back to GetWindowRect.
        if api.dwmapi is not None:
            hr = api.dwmapi.DwmGetWindowAttribute(
                hwnd, api.DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
            )
            if hr == 0 and rect.right > rect.left:
                return Rect(rect.left, rect.top, rect.right, rect.bottom)
        api.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return Rect(rect.left, rect.top, rect.right, rect.bottom)

    def _cloaked(self, hwnd: int) -> bool:
        if api.dwmapi is None:
            return False
        value = wintypes.DWORD(0)
        hr = api.dwmapi.DwmGetWindowAttribute(
            hwnd, api.DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
        return hr == 0 and value.value != 0

    def get_window(self, hwnd: int) -> Optional[WindowInfo]:
        if not hwnd or not api.user32.IsWindow(hwnd):
            return None
        pid = wintypes.DWORD(0)
        api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exstyle = api.user32.GetWindowLongW(hwnd, api.GWL_EXSTYLE)
        return WindowInfo(
            hwnd=int(hwnd),
            title=self._title(hwnd),
            class_name=self._class_name(hwnd),
            pid=int(pid.value),
            exe_path=self.process_exe_path(int(pid.value)),
            rect=self._rect(hwnd),
            visible=bool(api.user32.IsWindowVisible(hwnd)),
            minimized=bool(api.user32.IsIconic(hwnd)),
            cloaked=self._cloaked(hwnd),
            owner_hwnd=int(api.user32.GetWindow(hwnd, api.GW_OWNER) or 0),
            tool_window=bool(exstyle & api.WS_EX_TOOLWINDOW),
        )

    def list_windows(self, include_invisible: bool = False) -> list[WindowInfo]:
        handles: list[int] = []

        @api.WNDENUMPROC
        def _cb(hwnd, _lparam):
            handles.append(int(hwnd))
            return True

        api.user32.EnumWindows(_cb, 0)
        result = []
        for hwnd in handles:
            if not include_invisible and not api.user32.IsWindowVisible(hwnd):
                continue
            info = self.get_window(hwnd)
            if info is None:
                continue
            if not include_invisible and (info.cloaked or not info.title):
                continue
            result.append(info)
        return result


def selectable_windows(system: WindowSystem, own_pid: Optional[int] = None) -> list[WindowInfo]:
    """Visible, titled, non-tool windows a user could reasonably pick from."""
    own_pid = os.getpid() if own_pid is None else own_pid
    out = []
    for w in system.list_windows():
        if w.pid == own_pid or w.tool_window or not w.title.strip():
            continue
        if w.rect.width < 50 or w.rect.height < 50:
            continue
        out.append(w)
    out.sort(key=lambda w: (w.exe_name.lower(), w.title.lower()))
    return out


def looks_like_studio(window: WindowInfo) -> bool:
    """Heuristic used only to *highlight* likely candidates in the picker. The monitor's own window carries the
    Studio name in its title, so the process name decides first and our own process is never a candidate."""
    if window.pid == os.getpid() or "monitor screen" in (window.title or "").lower():
        return False
    exe = (window.exe_name or "").lower()
    if "tiktok" in exe and ("studio" in exe or "live" in exe):
        return True
    text = f"{window.title} {exe}".lower()
    return "tiktok" in text and ("live" in text or "studio" in text) and not exe.startswith(("python", "studiomonitor"))
