"""Text-evidence detectors: reconnecting / stream ended / missing source.

Classification of one OCR observation; temporal confirmation lives in
:class:`SustainedCondition`. Reconnecting is tracked as its own overlay state
(never converted into NOT_LIVE), with episode start, duration and recurrence.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..broadcast import phrase_in
from ..detection.rules import normalize_text


class ConnectionRules:
    def __init__(self, data: dict) -> None:
        self.verified = bool(data.get("verified", False))
        self.reconnecting = [normalize_text(p) for p in (data.get("reconnecting") or {}).get("any", [])]
        self.ended = [normalize_text(p) for p in (data.get("ended") or {}).get("any", [])]
        self.source_missing = [normalize_text(p) for p in (data.get("source_missing") or {}).get("any", [])]

    @classmethod
    def load(cls, path: Path) -> "ConnectionRules":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def classify(self, text: str) -> dict:
        """-> {"reconnecting": phrase|None, "ended": phrase|None, "source_missing": phrase|None}"""
        norm = normalize_text(text or "")
        out = {}
        for key, phrases in (("reconnecting", self.reconnecting), ("ended", self.ended), ("source_missing", self.source_missing)):
            out[key] = next((p for p in phrases if phrase_in(p, norm)), None)
        return out


@dataclass
class SustainedCondition:
    """Debounced boolean with sustain / recover intervals and episode tracking.

    ``update(active, valid, now)``: ``valid=False`` (no fresh frame) neither
    confirms nor clears; it pauses the clock. A gap of invalid observations
    longer than ``max_gap_seconds`` restarts the sustain timer, so an alert
    always rests on continuous recent evidence. Returns one of
    "confirmed" | "recovered" | None.
    """
    name: str
    sustain_seconds: float
    recover_seconds: float
    max_gap_seconds: float = 10.0
    invalid_since: Optional[float] = None
    confirmed: bool = False
    active_since: Optional[float] = None
    clear_since: Optional[float] = None
    episode_started: Optional[float] = None
    episodes: int = 0
    last_evidence: str = ""
    unknown: bool = True

    def update(self, active: bool, valid: bool, now: float, evidence: str = "") -> Optional[str]:
        if not valid:
            self.unknown = True
            if self.invalid_since is None:
                self.invalid_since = now
            return None
        self.unknown = False
        if self.invalid_since is not None:
            if now - self.invalid_since > self.max_gap_seconds:
                self.active_since = None          # evidence gap too long: start the sustain clock again
                self.clear_since = None
            self.invalid_since = None
        if active:
            self.clear_since = None
            self.last_evidence = evidence or self.last_evidence
            if self.active_since is None:
                self.active_since = now
            if not self.confirmed and now - self.active_since >= self.sustain_seconds:
                self.confirmed = True
                self.episode_started = self.active_since
                self.episodes += 1
                return "confirmed"
            return None
        self.active_since = None
        if self.confirmed:
            if self.clear_since is None:
                self.clear_since = now
            if now - self.clear_since >= self.recover_seconds:
                self.confirmed = False
                self.clear_since = None
                return "recovered"
        return None

    def duration(self, now: float) -> float:
        return (now - self.episode_started) if (self.confirmed and self.episode_started is not None) else 0.0

    def restart(self) -> None:
        """Forget partial (unconfirmed) evidence; keep a confirmed episode."""
        self.active_since = self.clear_since = self.invalid_since = None

    def reset(self) -> None:
        self.confirmed = False
        self.active_since = self.clear_since = self.episode_started = self.invalid_since = None
        self.unknown = True
