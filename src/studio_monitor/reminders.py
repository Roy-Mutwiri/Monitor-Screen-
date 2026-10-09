"""Not-live reminder engine.

Semantics (also documented in README):

* An *offline episode* starts when Studio is running and the broadcast state
  is **confirmed** NOT_LIVE (at Studio start, or after a confirmed
  LIVE -> NOT_LIVE transition). A confirmed LIVE state or Studio closing ends
  the episode and resets the reminder state.
* Confirmed offline time accumulates only between two consecutive confirmed
  NOT_LIVE observations that are at most ``max_gap_seconds`` apart
  (monotonic clock). Larger gaps (sleep, lock, UNKNOWN, capture loss, monitor
  downtime) contribute nothing; accumulation resumes after the next fresh
  NOT_LIVE confirmation. UNKNOWN never counts.
* When accumulated time reaches the threshold, one reminder event (with one
  delivery per subscribed bot) is created in the same SQLite transaction that
  marks it queued, so a crash or restart cannot produce a duplicate. Optional
  repeats fire every ``repeat_interval`` of *additional* confirmed offline
  time, up to ``repeat_max``.
* A reminder whose deliveries are still pending when the episode ends (LIVE
  confirmed or Studio closed) is cancelled and the reason recorded.
* State (episode id, accumulated seconds, reminders sent, pending event id,
  session pid) is persisted so a monitor restart continues the episode without
  counting downtime and without re-sending a reminder.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

from .broadcast import LiveState
from .queue import KIND_REMINDER, DeliveryQueue

STATE_KEY = "reminder_state"
EVT_REMINDER = "NOT_LIVE_REMINDER"
EVT_REMINDER_CANCELLED = "NOT_LIVE_REMINDER_CANCELLED"


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class ReminderState:
    episode_id: str = ""
    session_pid: int = 0
    accumulated_seconds: float = 0.0
    reminders_sent: int = 0
    pending_event_id: str = ""
    episode_started_utc: str = ""
    last_confirmed_utc: str = ""
    last_reminder_utc: str = ""
    last_cancel_reason: str = ""

    @property
    def active(self) -> bool:
        return bool(self.episode_id)


@dataclass
class ReminderDue:
    episode_id: str
    sequence: int          # 1 = first reminder, 2.. = repeats
    accumulated_seconds: float


@dataclass
class EngineOutput:
    due: Optional[ReminderDue] = None
    cancelled: list[tuple[str, str]] = field(default_factory=list)   # (event_id, reason)
    episode_started: bool = False
    episode_ended: str = ""


class OfflineReminderEngine:
    def __init__(self, queue: DeliveryQueue, *, threshold_seconds: float = 3600.0, max_gap_seconds: float = 30.0,
                 repeat_enabled: bool = False, repeat_interval_seconds: float = 3600.0, repeat_max: int = 3,
                 enabled: bool = True, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.queue = queue
        self.threshold = threshold_seconds
        self.max_gap = max_gap_seconds
        self.repeat_enabled = repeat_enabled
        self.repeat_interval = repeat_interval_seconds
        self.repeat_max = repeat_max
        self.enabled = enabled
        self.clock = clock
        self.mono = mono
        raw = queue.get_state(STATE_KEY) or {}
        raw.pop("pending_alert_id", None)   # pre-multi-bot field
        self.state = ReminderState(**{k: v for k, v in raw.items() if k in ReminderState.__dataclass_fields__})
        self._last_counted_mono: Optional[float] = None   # None -> need a fresh confirmation first
        self._prev_live: Optional[LiveState] = None

    # ------------------------------------------------------------------
    def _persist(self, conn=None) -> None:
        self.queue.set_state(STATE_KEY, asdict(self.state), conn)

    def _end_episode(self, reason: str, out: EngineOutput) -> None:
        st = self.state
        if not st.active:
            return
        if st.pending_event_id and self.queue.event_has_pending(st.pending_event_id):
            if self.queue.cancel_event(st.pending_event_id, reason):
                out.cancelled.append((st.pending_event_id, reason))
        out.episode_ended = reason
        self.state = ReminderState(last_cancel_reason=reason if out.cancelled else "")
        self._last_counted_mono = None
        self._persist()

    def _start_episode(self, pid: int, out: EngineOutput) -> None:
        now = self.clock()
        self.state = ReminderState(
            episode_id=f"EP-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}",
            session_pid=pid, episode_started_utc=_utc(now),
        )
        self._last_counted_mono = None
        out.episode_started = True
        self._persist()

    # ------------------------------------------------------------------
    def update(self, studio_running: bool, session_pid: int, confirmed: LiveState,
               state_confirmed_now: bool) -> EngineOutput:
        """One poll.

        studio_running      a Studio session is active (process confirmed alive)
        session_pid         pid of that session (0 if none)
        confirmed           the engine's current *confirmed* broadcast state
        state_confirmed_now True if this poll was itself a confirming observation
                            of ``confirmed`` (fresh evidence), False otherwise
        """
        out = EngineOutput()
        st = self.state
        now_mono = self.mono()

        if not studio_running:
            self._end_episode("studio closed", out)
            self._prev_live = None
            return out

        # Episode persisted from a previous monitor run for a different Studio process -> stale.
        if st.active and st.session_pid and session_pid and st.session_pid != session_pid:
            self._end_episode("studio restarted", out)
            st = self.state

        if confirmed == LiveState.LIVE:
            if st.active:
                self._end_episode("broadcast went live", out)
            self._last_counted_mono = None
            self._prev_live = confirmed
            return out

        if confirmed != LiveState.NOT_LIVE or not state_confirmed_now:
            # UNKNOWN, or a NOT_LIVE poll without fresh confirmation: pause accumulation.
            self._last_counted_mono = None
            self._prev_live = confirmed
            return out

        # Confirmed NOT_LIVE with fresh evidence.
        if not st.active:
            if not self.enabled:
                self._prev_live = confirmed
                return out
            self._start_episode(session_pid, out)
            st = self.state
        if self._last_counted_mono is not None:
            delta = now_mono - self._last_counted_mono
            if 0 <= delta <= self.max_gap:
                st.accumulated_seconds += delta
        self._last_counted_mono = now_mono
        st.last_confirmed_utc = _utc(self.clock())
        self._prev_live = confirmed

        due = self._reminder_due(st)
        if due is not None:
            out.due = due
        else:
            self._persist()
        return out

    def _reminder_due(self, st: ReminderState) -> Optional[ReminderDue]:
        if not self.enabled:
            return None
        if st.reminders_sent == 0:
            if st.accumulated_seconds >= self.threshold:
                return ReminderDue(st.episode_id, 1, st.accumulated_seconds)
            return None
        if not self.repeat_enabled or st.reminders_sent > self.repeat_max:
            return None
        if st.accumulated_seconds >= self.threshold + st.reminders_sent * self.repeat_interval:
            return ReminderDue(st.episode_id, st.reminders_sent + 1, st.accumulated_seconds)
        return None

    def enqueue_reminder(self, due: ReminderDue, payload: dict, screenshot_path: str, event_id: str,
                         details: dict, targets: list, category: str) -> int:
        """Atomically create the reminder event + per-bot deliveries, record the
        history row and mark the reminder as queued. Returns deliveries created."""
        st = self.state
        with self.queue.transaction() as conn:
            n = self.queue.create_event(event_id, KIND_REMINDER, category, payload, screenshot_path, targets,
                                        label=f"Not-live reminder {due.sequence}", conn=conn)
            st.reminders_sent = due.sequence
            st.pending_event_id = event_id
            st.last_reminder_utc = _utc(self.clock())
            conn.execute(
                "INSERT OR REPLACE INTO activity_events VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, EVT_REMINDER, "", due.episode_id, st.last_reminder_utc, json.dumps(details),
                 screenshot_path, None, self.clock()),
            )
            self._persist(conn)
        return n

    # -- GUI helpers ------------------------------------------------------
    def remaining_seconds(self) -> Optional[float]:
        st = self.state
        if not st.active or not self.enabled:
            return None
        if st.reminders_sent == 0:
            return max(0.0, self.threshold - st.accumulated_seconds)
        if not self.repeat_enabled or st.reminders_sent > self.repeat_max:
            return None
        return max(0.0, self.threshold + st.reminders_sent * self.repeat_interval - st.accumulated_seconds)

    @property
    def accumulating(self) -> bool:
        return self.state.active and self._last_counted_mono is not None
