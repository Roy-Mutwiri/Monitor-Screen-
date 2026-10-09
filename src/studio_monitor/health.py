"""Health model and the Telegram health-alert debounce.

Dimensions are tracked separately (application/session, capture, OCR,
broadcast, delivery) and shown immediately in the GUI. Telegram gets a
degradation alert only after it persists ``degrade_after`` seconds, one per
episode, and a recovery only after ``recover_after`` seconds of stable health
and only if the degradation was alerted. The episode is persisted so a restart
does not re-announce an already alerted degradation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

STATE_KEY = "health_episode"


@dataclass
class HealthSnapshot:
    session: str = "NOT_RUNNING"        # Studio application state
    capture: str = "NONE"               # OK | DEGRADED | NONE
    capture_reason: str = ""
    capture_backend: str = ""
    last_valid_frame_at: float = 0.0
    ocr: str = "OK"                     # OK | FAILING
    ocr_reason: str = ""
    broadcast: str = "UNKNOWN"
    delivery: str = "OK"                # OK | FAILING
    delivery_reason: str = ""

    @property
    def degraded(self) -> bool:
        return self.capture == "DEGRADED" or self.ocr == "FAILING"

    @property
    def degraded_reason(self) -> str:
        if self.capture == "DEGRADED":
            return self.capture_reason
        if self.ocr == "FAILING":
            return self.ocr_reason
        return ""


@dataclass
class HealthAlert:
    kind: str          # "degraded" | "recovered"
    reason: str
    since: float       # wall clock when the episode began
    duration: float


@dataclass
class _Episode:
    degraded: bool = False
    reason: str = ""
    since: float = 0.0           # wall clock
    since_mono: float = 0.0
    alerted: bool = False
    stable_since_mono: Optional[float] = None


class HealthAlertPolicy:
    def __init__(self, queue, *, degrade_after: float = 15.0, recover_after: float = 10.0,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.queue = queue
        self.degrade_after = degrade_after
        self.recover_after = recover_after
        self.clock = clock
        self.mono = mono
        raw = queue.get_state(STATE_KEY) or {}
        self.ep = _Episode(degraded=bool(raw.get("degraded")), reason=str(raw.get("reason", "")),
                           since=float(raw.get("since", 0.0)), since_mono=mono(), alerted=bool(raw.get("alerted")))
        if self.ep.degraded and not self.ep.alerted:
            # unalerted degradation from a previous run: start the clock afresh
            self.ep = _Episode()

    def _persist(self) -> None:
        self.queue.set_state(STATE_KEY, {"degraded": self.ep.degraded, "reason": self.ep.reason,
                                         "since": self.ep.since, "alerted": self.ep.alerted})

    def update(self, degraded: bool, reason: str) -> Optional[HealthAlert]:
        now, mono = self.clock(), self.mono()
        ep = self.ep
        if degraded:
            ep.stable_since_mono = None
            if not ep.degraded:
                ep.degraded, ep.reason, ep.since, ep.since_mono, ep.alerted = True, reason, now, mono, False
                self._persist()
            elif reason and reason != ep.reason:
                ep.reason = reason          # same episode, updated cause; no new message
                self._persist()
            if not ep.alerted and mono - ep.since_mono >= self.degrade_after:
                ep.alerted = True
                self._persist()
                return HealthAlert("degraded", ep.reason, ep.since, mono - ep.since_mono)
            return None
        # healthy
        if not ep.degraded:
            return None
        if ep.stable_since_mono is None:
            ep.stable_since_mono = mono
            return None
        if mono - ep.stable_since_mono >= self.recover_after:
            alerted, reason_, since, dur = ep.alerted, ep.reason, ep.since, mono - ep.since_mono
            self.ep = _Episode()
            self._persist()
            if alerted:
                return HealthAlert("recovered", reason_, since, dur)
        return None

    @property
    def episode(self) -> dict:
        return asdict(self.ep)
