from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from studio_monitor.bots import BotRegistry  # noqa: E402
from studio_monitor.config import AppConfig, TargetIdentity  # noqa: E402
from studio_monitor.credentials import MemoryCredentialStore  # noqa: E402
from studio_monitor.detection.rules import load_rules  # noqa: E402
from studio_monitor.ocr.base import OcrResult  # noqa: E402
from studio_monitor.win32.capture import Capture  # noqa: E402
from studio_monitor.win32.windows import Rect, WindowInfo  # noqa: E402

RULES = Path(__file__).resolve().parents[1] / "rules" / "studio_rules.json"
TOKEN_A = "123456789:AAFakeTokenForTests_abcdefghijklmnop"
TOKEN_B = "987654321:BBFakeTokenForTests_abcdefghijklmnop"
TOKEN_C = "555555555:CCFakeTokenForTests_abcdefghijklmnop"


def make_token(n: int) -> str:
    return f"{100000000 + n}:ZZFakeTokenForTests_{n:04d}abcdefghijklmnop"


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
    start_times: dict[int, float] = field(default_factory=dict)   # pid -> process creation time
    locked: bool = False
    covering: dict[int, int] = field(default_factory=dict)        # hwnd -> hwnd of a window covering it

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

    def process_start_time(self, pid):
        return self.start_times.get(pid, 1_000.0 + pid)

    def window_at_point(self, x, y):
        for w in self.windows.values():
            r = w.rect
            if w.visible and r.left <= x < r.right and r.top <= y < r.bottom:
                return self.covering.get(w.hwnd, w.hwnd)
        return 0

    def desktop_locked(self):
        return self.locked

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
        return Capture(fake_frame(window.rect.width, window.rect.height), window, "printwindow", not self.unreliable,
                       hwnd=window.hwnd)


def fake_frame(width: int, height: int, color=(40, 40, 40)) -> Image.Image:
    """A non-uniform frame (uniform bitmaps are rejected as blank): dark
    background with a lighter block in the centre; pixel (0,0) stays ``color``."""
    from PIL import ImageDraw
    w, h = max(8, width), max(8, height)
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    d.rectangle((w * 0.3, h * 0.3, w * 0.45, h * 0.45), fill=(200, 200, 200))
    return img


class FakeOcr:
    """Maps image size -> text (set via ``texts``); default otherwise."""
    name = "fake"

    def __init__(self):
        self.texts: dict[tuple[int, int], str] = {}
        self.default = ""
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        return OcrResult(self.texts.get(image.size, self.default), backend=self.name)


class FakeTransport:
    """Scripted Telegram transport. ``responses`` may be a list (shared) or a
    dict token->list. Records every request (url, data, headers)."""

    def __init__(self, responses=None, per_token=None):
        self.responses = list(responses or [])
        self.per_token = {k: list(v) for k, v in (per_token or {}).items()}
        self.requests: list[tuple[str, Optional[bytes], dict]] = []
        self.default = (200, {"ok": True, "result": {"message_id": 1}})

    def __call__(self, url, data, headers, timeout):
        self.requests.append((url, data, headers))
        token = url.split("/bot", 1)[1].split("/", 1)[0] if "/bot" in url else ""
        bucket = self.per_token.get(token)
        if bucket:
            status, body = bucket.pop(0)
        elif self.responses:
            status, body = self.responses.pop(0)
        else:
            status, body = self.default
        if callable(body):
            body = body()
        if isinstance(body, Exception):
            raise body
        return status, json.dumps(body).encode()

    def sent_to(self, token: str) -> list[tuple[str, Optional[bytes]]]:
        return [(u, d) for (u, d, h) in self.requests if f"/bot{token}/" in u and not u.endswith("/getMe")]


def all_deliveries(queue) -> list[dict]:
    """Flat view of deliveries joined with their events (test helper)."""
    rows = queue._conn.execute(
        "SELECT d.id, d.event_id, e.payload, e.evidence_path, d.status, e.kind, d.last_error, d.bot_id, d.bot_name, "
        "d.chat_id, d.thread_id, d.message_id, d.attempts FROM deliveries d JOIN events e ON e.event_id=d.event_id "
        "ORDER BY d.id").fetchall()
    return [dict(id=r[0], incident_id=r[1], event_id=r[1], payload=json.loads(r[2]), screenshot_path=r[3], status=r[4],
                 kind=r[5], last_error=r[6], bot_id=r[7], bot_name=r[8], chat_id=r[9], thread_id=r[10],
                 message_id=r[11], attempts=r[12]) for r in rows]


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
    c.telegram.fingerprint_salt = "test-salt"
    return c


@pytest.fixture
def store():
    return MemoryCredentialStore()


@pytest.fixture
def registry(cfg, tmp_path, store):
    """Registry with one enabled 'Default Bot' subscribed to everything."""
    cfg_path = tmp_path / "config.json"
    reg = BotRegistry(cfg, store, save=lambda: cfg.save(cfg_path))
    reg.add("Default Bot", TOKEN_A, "42")
    return reg
