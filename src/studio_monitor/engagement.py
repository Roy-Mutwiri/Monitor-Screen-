"""Engagement readings from Studio's own on-screen counters (viewers, likes)
in the live-status OCR text. Parsed numbers are observations for reports and
/status only; they never drive alerts or actions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_NUM = r"(\d[\d,.]*)\s*([KkMm])?"
_VIEWERS = re.compile(rf"{_NUM}\s*(?:viewers?|watching)\b", re.I)
_LIKES = re.compile(rf"{_NUM}\s*likes?\b", re.I)


def parse_count(num: str, suffix: Optional[str]) -> Optional[int]:
    try:
        base = float(num.replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)
    return int(base * mult)


def parse_engagement(text: str) -> dict:
    out: dict = {}
    m = _VIEWERS.search(text or "")
    if m:
        v = parse_count(m.group(1), m.group(2))
        if v is not None:
            out["viewers"] = v
    m = _LIKES.search(text or "")
    if m:
        v = parse_count(m.group(1), m.group(2))
        if v is not None:
            out["likes"] = v
    return out


@dataclass
class EngagementStats:
    episode_id: str = ""
    samples: int = 0
    viewers_latest: Optional[int] = None
    viewers_peak: Optional[int] = None
    viewers_sum: int = 0
    likes_latest: Optional[int] = None
    likes_peak: Optional[int] = None
    history: list = field(default_factory=list)      # (ts, viewers) capped

    def observe(self, ts: float, reading: dict) -> None:
        v, l = reading.get("viewers"), reading.get("likes")
        if v is not None:
            self.samples += 1
            self.viewers_latest, self.viewers_sum = v, self.viewers_sum + v
            self.viewers_peak = v if self.viewers_peak is None else max(self.viewers_peak, v)
            self.history.append((ts, v))
            if len(self.history) > 720:
                del self.history[0]
        if l is not None:
            self.likes_latest = l
            self.likes_peak = l if self.likes_peak is None else max(self.likes_peak, l)

    @property
    def viewers_avg(self) -> Optional[float]:
        return (self.viewers_sum / self.samples) if self.samples else None

    def summary(self) -> str:
        if not self.samples and self.likes_latest is None:
            return ""
        parts = []
        if self.samples:
            parts.append(f"viewers now {self.viewers_latest}, peak {self.viewers_peak}, avg {self.viewers_avg:.0f}")
        if self.likes_latest is not None:
            parts.append(f"likes {self.likes_latest}")
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return {"episode_id": self.episode_id, "samples": self.samples, "viewers_latest": self.viewers_latest,
                "viewers_peak": self.viewers_peak, "viewers_avg": round(self.viewers_avg, 1) if self.samples else None,
                "likes_latest": self.likes_latest, "likes_peak": self.likes_peak}
