"""Window lifecycle tracking.

States
------
RUNNING   Studio is visible and captures are reliable.
DEGRADED  Studio exists but we cannot reliably see it (minimized, cloaked,
          off-screen, capture failing, or a screen-grab fallback while another
          window is in front).
LOST      The window or its process is gone; we are waiting to rediscover it.
STOPPED   Monitoring is not active.

The tracker never focuses, restores or otherwise touches the Studio windows.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .config import TargetIdentity
from .target import identity_from_window, rediscover, validate_handle
from .win32.windows import Rect, WindowInfo, WindowSystem


class Status(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    LOST = "LOST"


@dataclass
class TrackerState:
    status: Status = Status.STOPPED
    reason: str = ""
    window: Optional[WindowInfo] = None
    last_rect: Optional[Rect] = None
    lost_since: Optional[float] = None
    rediscovered_count: int = 0
    events: list[str] = field(default_factory=list)


class WindowTracker:
    def __init__(self, system: WindowSystem, identity: TargetIdentity,
                 clock: Callable[[], float] = time.time,
                 on_identity_change: Optional[Callable[[TargetIdentity], None]] = None) -> None:
        self.system = system
        self.identity = identity
        self.clock = clock
        self.state = TrackerState()
        self._on_identity_change = on_identity_change

    # ------------------------------------------------------------------
    def _set(self, status: Status, reason: str = "", window: Optional[WindowInfo] = None) -> None:
        changed = status != self.state.status or reason != self.state.reason
        self.state.status = status
        self.state.reason = reason
        self.state.window = window
        if window is not None:
            if self.state.last_rect is not None and window.rect != self.state.last_rect:
                self.state.events.append(
                    f"window moved/resized to {window.rect.width}x{window.rect.height} at "
                    f"({window.rect.left},{window.rect.top})"
                )
            self.state.last_rect = window.rect
        if changed:
            self.state.events.append(f"{status.value}: {reason}" if reason else status.value)

    def drain_events(self) -> list[str]:
        events, self.state.events = self.state.events, []
        return events

    # ------------------------------------------------------------------
    def poll(self) -> TrackerState:
        """Refresh the view of the Studio window. Call once per monitoring tick."""
        result = validate_handle(self.system, self.identity)
        if not result.ok:
            found = rediscover(self.system, self.identity)
            if found is None:
                if self.state.lost_since is None:
                    self.state.lost_since = self.clock()
                self._set(Status.LOST, f"Studio window unavailable ({result.reason}); waiting for it to reappear")
                return self.state
            self._adopt(found, result.reason)
            result = validate_handle(self.system, self.identity)
            if not result.ok:
                self._set(Status.LOST, f"rediscovered window failed validation: {result.reason}")
                return self.state

        window = result.window
        assert window is not None
        self.state.lost_since = None
        if window.minimized:
            self._set(Status.DEGRADED, "Studio is minimized; cannot capture", window)
        elif window.cloaked or not window.visible:
            self._set(Status.DEGRADED, "Studio window is hidden/cloaked", window)
        elif not self._on_screen(window.rect):
            self._set(Status.DEGRADED, "Studio window is off-screen", window)
        else:
            self._set(Status.RUNNING, "", window)
        return self.state

    def report_capture(self, ok: bool, reliable: bool, note: str = "") -> None:
        """Called by the monitor after attempting a capture for this tick."""
        if self.state.status != Status.RUNNING:
            return
        if not ok:
            self._set(Status.DEGRADED, "capture failed; Studio may be hidden or not rendering", self.state.window)
        elif not reliable:
            self._set(Status.DEGRADED, note or "capture may not reflect Studio contents", self.state.window)

    def _adopt(self, window: WindowInfo, why: str) -> None:
        old = self.identity
        new = identity_from_window(window, self.system)
        self.identity = new
        self.state.rediscovered_count += 1
        self.state.events.append(
            f"rediscovered Studio window {window.describe()} (old handle 0x{old.hwnd:X} invalid: {why})"
        )
        if self._on_identity_change:
            self._on_identity_change(new)

    def _on_screen(self, rect: Rect) -> bool:
        screen = self.system.virtual_screen()
        if rect.width == 0 or rect.height == 0:
            return False
        # Windows parks minimized windows at -32000; also catch windows fully outside.
        return not (rect.right <= screen.left or rect.left >= screen.right
                    or rect.bottom <= screen.top or rect.top >= screen.bottom)

    def stop(self) -> None:
        self._set(Status.STOPPED, "")
