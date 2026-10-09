"""Studio *application session* tracking (opened / closed notifications).

A session is one run of the Studio main process. The session key is the
process id of the validated main window's process, so window recreation,
dialogs, moves, minimizing and temporary capture failures (same pid) never
look like a new session. A session ends only when that process is confirmed
gone for ``close_debounce_seconds`` (or is replaced by a different Studio
process, which is a restart: ended session + new session).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import TargetIdentity
from .win32.windows import WindowSystem

EVT_OPENED = "STUDIO_OPENED"
EVT_ALREADY_RUNNING = "STUDIO_ALREADY_RUNNING"
EVT_CLOSED = "STUDIO_CLOSED"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionEvent:
    type: str
    session_id: str
    pid: int
    ts: float                      # wall clock
    ts_utc: str
    screenshot_available: bool = False
    note: str = ""


@dataclass
class Session:
    session_id: str
    pid: int
    exe_name: str
    started_at: float
    opened_sent: bool = False
    first_seen_mono: float = 0.0


@dataclass
class SessionState:
    app_state: str = "NOT_RUNNING"   # NOT_RUNNING | STARTING | RUNNING | CLOSING
    session: Optional[Session] = None
    closing_since_mono: Optional[float] = None
    last_event: str = ""
    events: list[SessionEvent] = field(default_factory=list)


class StudioSessionTracker:
    def __init__(self, system: WindowSystem, *, close_debounce_seconds: float = 10.0,
                 open_screenshot_timeout_seconds: float = 30.0, notify_already_running: bool = True,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.system = system
        self.close_debounce = close_debounce_seconds
        self.open_timeout = open_screenshot_timeout_seconds
        self.notify_already_running = notify_already_running
        self.clock = clock
        self.mono = mono
        self.state = SessionState()
        self._first_update = True

    # ------------------------------------------------------------------
    def _process_is_studio(self, pid: int, exe_name: str) -> bool:
        """Process-level evidence: alive *and* still the Studio executable
        (pids are recycled by Windows)."""
        if not pid or not self.system.process_alive(pid):
            return False
        path = self.system.process_exe_path(pid)
        if not path:
            return True  # alive but unreadable (elevated); trust liveness
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        return name.lower() == exe_name.lower()

    def _emit(self, type_: str, session: Session, screenshot: bool, note: str = "") -> SessionEvent:
        now = self.clock()
        ev = SessionEvent(type_, session.session_id, session.pid, now, _iso(now), screenshot, note)
        self.state.events.append(ev)
        self.state.last_event = f"{type_} ({datetime.fromtimestamp(now).strftime('%H:%M:%S')})"
        return ev

    def drain_events(self) -> list[SessionEvent]:
        evs, self.state.events = self.state.events, []
        return evs

    # ------------------------------------------------------------------
    def update(self, identity: TargetIdentity, window_present: bool, screenshot_ok: bool) -> SessionState:
        """Call once per poll.

        identity        current (possibly rediscovered) target identity
        window_present  the tracker validated a Studio window this poll
        screenshot_ok   a reliable capture of the main window succeeded this poll
        """
        now_mono = self.mono()
        st = self.state
        current_pid = identity.pid if window_present else 0
        running_pid = 0
        if current_pid and self._process_is_studio(current_pid, identity.exe_name):
            running_pid = current_pid
        elif st.session and self._process_is_studio(st.session.pid, st.session.exe_name):
            running_pid = st.session.pid        # window gone/hidden, process still alive

        # --- restart: a different Studio process replaced the one we track
        if st.session and running_pid and running_pid != st.session.pid:
            self._close_session("process replaced by a new Studio process")

        if running_pid:
            st.closing_since_mono = None
            if st.session is None:
                st.session = Session(f"SES-{datetime.fromtimestamp(self.clock()):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}",
                                     running_pid, identity.exe_name, self.clock(), first_seen_mono=now_mono)
                if self._first_update and self.notify_already_running:
                    st.session.opened_sent = True
                    st.app_state = "RUNNING"
                    self._emit(EVT_ALREADY_RUNNING, st.session, screenshot_ok)
                elif self._first_update:
                    st.session.opened_sent = True   # no notification wanted; but it is not a fresh start
                    st.app_state = "RUNNING"
                else:
                    st.app_state = "STARTING"
            if st.session and not st.session.opened_sent:
                if screenshot_ok:
                    st.session.opened_sent = True
                    st.app_state = "RUNNING"
                    self._emit(EVT_OPENED, st.session, True)
                elif now_mono - st.session.first_seen_mono >= self.open_timeout:
                    st.session.opened_sent = True
                    st.app_state = "RUNNING"
                    self._emit(EVT_OPENED, st.session, False,
                               "no usable screenshot within the timeout; capture unavailable")
            elif st.session:
                st.app_state = "RUNNING"
        else:
            if st.session is not None:
                if st.closing_since_mono is None:
                    st.closing_since_mono = now_mono
                    st.app_state = "CLOSING"
                elif now_mono - st.closing_since_mono >= self.close_debounce:
                    self._close_session("process exited")
            else:
                st.app_state = "NOT_RUNNING"
        self._first_update = False
        return st

    def _close_session(self, why: str) -> None:
        st = self.state
        if st.session is None:
            return
        self._emit(EVT_CLOSED, st.session, False, why)
        st.session = None
        st.closing_since_mono = None
        st.app_state = "NOT_RUNNING"

    @property
    def running(self) -> bool:
        return self.state.session is not None and self.state.app_state in ("STARTING", "RUNNING")

    @property
    def session_id(self) -> str:
        return self.state.session.session_id if self.state.session else ""
