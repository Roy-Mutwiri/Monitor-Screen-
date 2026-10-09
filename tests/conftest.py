from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from studio_monitor.config import AppConfig, TargetIdentity  # noqa: E402
from studio_monitor.detection.rules import load_rules  # noqa: E402
from studio_monitor.ocr.base import OcrResult  # noqa: E402
from studio_monitor.win32.capture import Capture  # noqa: E402
from studio_monitor.win32.windows import Rect, WindowInfo  # noqa: E402

RULES = Path(__file__).resolve().parents[1] / "rules" / "studio_rules.json"


def make_window(hwnd=0x1001, title="TikTok LIVE Studio", exe="C:/Program Files/TikTok LIVE Studio/1.36.6/TikTok LIVE Studio.exe",
                pid=4242, cls="Chrome_WidgetWin_1", rect=Rect(100, 100, 1380, 820), visible=True, minimized=False,
                cloaked=False, owner=0, tool=False) -> WindowInfo:
    return WindowInfo(hwnd, title, cls, pid, exe, rect, visible, minimized, cloaked, owner, tool)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


@dataclass
class FakeWindowSystem:
    windows: dict[int, WindowInfo] = field(default_factory=dict)
    alive: set[int] = field(default_factory=set)
    children: dict[int, set[int]] = field(default_factory=dict)
    foreground: int = 0
    screen: Rect = Rect(0, 0, 2560, 1440)

    def add(self, w: WindowInfo):
        self.windows[w.hwnd] = w
        self.alive.add(w.pid)
        return w

    def remove(self, hwnd: int):
        self.windows.pop(hwnd, None)

    def list_windows(self, include_invisible=False):
        return [w for w in self.windows.values() if include_invisible or w.visible]

    def get_window(self, hwnd):
        return self.windows.get(hwnd)

    def is_window(self, hwnd):
        return hwnd in self.windows

    def process_alive(self, pid):
        return pid in self.alive

    def process_exe_path(self, pid):
        for w in self.windows.values():
            if w.pid == pid:
                return w.exe_path
        return ""

    def process_tree(self, root_pid):
        tree = {root_pid} if root_pid in self.alive else set()
        for child in self.children.get(root_pid, ()):
            tree |= self.process_tree(child)
        return tree

    def foreground_window(self):
        return self.foreground

    def virtual_screen(self):
        return self.screen


class FakeCapturer:
    """Returns a blank image per window; records calls."""

    def __init__(self, fail_hwnds=(), unreliable=False):
        self.fail_hwnds = set(fail_hwnds)
        self.unreliable = unreliable
        self.calls: list[int] = []

    def capture(self, window, foreground_hwnd=0):
        self.calls.append(window.hwnd)
        if window.hwnd in self.fail_hwnds or window.minimized:
            return None
        img = Image.new("RGB", (max(2, window.rect.width), max(2, window.rect.height)), (40, 40, 40))
        return Capture(img, window, "printwindow", not self.unreliable)


class FakeOcr:
    """Maps window hwnd -> text (set via ``texts``); default empty."""
    name = "fake"

    def __init__(self):
        self.texts: dict[tuple[int, int], str] = {}  # (width,height) -> text
        self.default = ""
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        return OcrResult(self.texts.get(image.size, self.default), backend=self.name)


@pytest.fixture
def rules():
    return load_rules(RULES)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def studio_identity():
    return TargetIdentity(hwnd=0x1001, pid=4242, exe_path="C:/Program Files/TikTok LIVE Studio/1.36.6/TikTok LIVE Studio.exe",
                          exe_name="TikTok LIVE Studio.exe", class_name="Chrome_WidgetWin_1", title="TikTok LIVE Studio")


@pytest.fixture
def cfg(tmp_path, studio_identity):
    c = AppConfig()
    c.data_dir = str(tmp_path / "data")
    c.machine_label = "test-pc"
    c.target = studio_identity
    c.detection.confirm_polls = 1
    c.telegram.bot_token = "123:abc"
    c.telegram.chat_id = "42"
    return c
