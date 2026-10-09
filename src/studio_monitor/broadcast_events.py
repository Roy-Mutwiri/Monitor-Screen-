"""Broadcast episode tracking: turns confirmed broadcast-state transitions into
at most one "has gone live" event per broadcast, with honest wording when the
transition itself was not observed.

Persisted (kv ``broadcast_episode``): last confirmed state, episode id,
whether the next start alert is armed. Rules:

* First confirmed LIVE in a monitor run -> ``already_live`` (distinct wording),
  never a "gone live"; it also re-uses a persisted LIVE episode if the
  previous run ended while live (restart deduplication).
* Confirmed NOT_LIVE arms the next start alert and clears the episode.
* Armed + confirmed LIVE -> ``started`` when the previous confirmed state was
  NOT_LIVE and the observations were continuous; ``started_after_gap`` when an
  UNKNOWN gap longer than the max observation gap separated them (the
  transition happened sometime during the gap; its time is not asserted).
* UNKNOWN -> LIVE while the same LIVE episode is already announced -> nothing
  (observation resumed).
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable, Optional

from .broadcast import LiveState

STATE_KEY = "broadcast_episode"


@dataclass
class EpisodeState:
    last_confirmed: str = LiveState.UNKNOWN.value
    episode_id: str = ""
    armed: bool = True
    announced: bool = False
    live_since_utc: str = ""


@dataclass
class BroadcastEvent:
    kind: str                 # started | started_after_gap | already_live | ended
    episode_id: str
    gap_seconds: float = 0.0


class BroadcastEpisodeTracker:
    def __init__(self, queue, max_gap_seconds: float = 30.0, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.queue = queue
        self.max_gap = max_gap_seconds
        self.clock = clock
        self.mono = mono
        raw = queue.get_state(STATE_KEY) or {}
        self.state = EpisodeState(**{k: v for k, v in raw.items() if k in EpisodeState.__dataclass_fields__})
        self._first_in_run = True
        self._last_confirmed_mono: Optional[float] = None
        self._prev_confirmed: Optional[LiveState] = None

    def _persist(self) -> None:
        self.queue.set_state(STATE_KEY, asdict(self.state))

    def _new_episode(self) -> str:
        return f"BC-{datetime.fromtimestamp(self.clock()):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"

    def observe(self, confirmed: LiveState, fresh: bool) -> Optional[BroadcastEvent]:
        """Feed the engine's confirmed state each poll. ``fresh`` means this
        poll itself confirmed that state (a valid observation)."""
        if not fresh or confirmed == LiveState.UNKNOWN:
            return None
        now_mono = self.mono()
        gap = (now_mono - self._last_confirmed_mono) if self._last_confirmed_mono is not None else 0.0
        prev = self._prev_confirmed
        self._prev_confirmed = confirmed
        self._last_confirmed_mono = now_mono
        st = self.state
        event: Optional[BroadcastEvent] = None
        if confirmed == LiveState.NOT_LIVE:
            if st.last_confirmed == LiveState.LIVE.value and st.episode_id:
                event = BroadcastEvent("ended", st.episode_id)
            st.last_confirmed = LiveState.NOT_LIVE.value
            st.episode_id, st.armed, st.announced, st.live_since_utc = "", True, False, ""
            self._first_in_run = False
            self._persist()
            return event
        # confirmed LIVE
        if self._first_in_run:
            self._first_in_run = False
            if st.last_confirmed == LiveState.LIVE.value and st.episode_id:
                episode = st.episode_id          # same broadcast as before the restart
            else:
                episode = self._new_episode()
            st.last_confirmed, st.episode_id, st.armed, st.announced = LiveState.LIVE.value, episode, False, True
            st.live_since_utc = st.live_since_utc or _utc(self.clock())
            self._persist()
            return BroadcastEvent("already_live", episode)
        if st.last_confirmed == LiveState.LIVE.value and st.episode_id:
            return None                           # LIVE observation resumed; same episode
        if not st.armed:
            return None
        episode = self._new_episode()
        st.last_confirmed, st.episode_id, st.armed, st.announced = LiveState.LIVE.value, episode, False, True
        st.live_since_utc = _utc(self.clock())
        self._persist()
        after_gap = prev != LiveState.NOT_LIVE or gap > self.max_gap
        return BroadcastEvent("started_after_gap" if after_gap else "started", episode, gap)

    def note_gap(self) -> None:
        """Called on polls without a valid observation; nothing to persist."""
        self._prev_confirmed = None


def _utc(ts: float) -> str:
    from datetime import timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
