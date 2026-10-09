"""Incident identity, de-duplication and confirmation.

A *detection* is one positive OCR match in one poll. An *incident* is a popup
that has been confirmed in ``confirm_polls`` consecutive polls and has not been
alerted on within the cooldown. The same popup staying on screen is one
incident; it is only re-alerted after it disappears for ``resolve_after``
seconds and reappears, or after the cooldown has elapsed.
"""
from __future__ import annotations

import difflib
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .detection.rules import normalize_text


def new_incident_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return f"INC-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def fingerprint(category: str, text: str) -> str:
    """Stable key for a popup: category + normalized text with digits removed
    (counters/timers inside popups change between polls)."""
    norm = "".join(ch for ch in normalize_text(text) if not ch.isdigit())
    norm = " ".join(norm.split())
    return hashlib.sha1(f"{category}|{norm}".encode("utf-8")).hexdigest()[:16]


def similar(a: str, b: str, threshold: float = 0.85) -> bool:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


@dataclass
class Incident:
    incident_id: str
    category: str
    label: str
    text: str
    fingerprint: str
    first_seen: float
    last_seen: float
    last_alerted: float = 0.0
    alerts_sent: int = 0
    window_title: str = ""
    is_dialog: bool = False
    screenshot_path: str = ""
    manual_attention: bool = False
    gone_reported: bool = False   # popup not seen for resolve_after -> reported once to the incident engine


@dataclass
class _Pending:
    fingerprint: str
    category: str
    text: str
    count: int = 1
    last_seen: float = 0.0


@dataclass
class DedupDecision:
    alert: bool
    incident: Optional[Incident]
    reason: str


class IncidentTracker:
    def __init__(self, confirm_polls: int = 2, cooldown_seconds: float = 600.0,
                 resolve_after_seconds: float = 30.0, clock: Callable[[], float] = time.time) -> None:
        self.confirm_polls = max(1, confirm_polls)
        self.cooldown = cooldown_seconds
        self.resolve_after = resolve_after_seconds
        self.clock = clock
        self.active: dict[str, Incident] = {}
        self._pending: dict[str, _Pending] = {}
        self.history: list[Incident] = []
        self.newly_gone: list[Incident] = []

    # ------------------------------------------------------------------
    def _find_similar(self, category: str, text: str) -> Optional[Incident]:
        for inc in self.active.values():
            if inc.category == category and similar(inc.text, text):
                return inc
        return None

    def observe(self, category: str, label: str, text: str, *, manual_attention: bool = False,
                window_title: str = "", is_dialog: bool = False) -> DedupDecision:
        """Record a positive detection for this poll and decide whether to alert."""
        now = self.clock()
        fp = fingerprint(category, text)

        # Known incident still on screen (exact or fuzzy match)?
        inc = self.active.get(fp) or self._find_similar(category, text)
        if inc is not None:
            gone_for = now - inc.last_seen
            inc.last_seen = now
            inc.gone_reported = False
            if gone_for >= self.resolve_after:
                # It went away and came back: a fresh occurrence.
                return self._alert(inc, now, "popup reappeared after being resolved", renew_id=True)
            if now - inc.last_alerted >= self.cooldown:
                return self._alert(inc, now, "cooldown elapsed while popup persists")
            return DedupDecision(False, inc, "duplicate of active incident")

        # Not yet an incident: count consecutive confirmations.
        pend = self._pending.get(fp)
        if pend is None:
            pend = _Pending(fp, category, text, 1, now)
            self._pending[fp] = pend
        else:
            pend.count += 1
            pend.last_seen = now
        if pend.count < self.confirm_polls:
            return DedupDecision(False, None, f"awaiting confirmation ({pend.count}/{self.confirm_polls})")

        del self._pending[fp]
        inc = Incident(
            incident_id=new_incident_id(), category=category, label=label, text=text,
            fingerprint=fp, first_seen=now, last_seen=now, window_title=window_title,
            is_dialog=is_dialog, manual_attention=manual_attention,
        )
        self.active[fp] = inc
        return self._alert(inc, now, "new incident")

    def _alert(self, inc: Incident, now: float, reason: str, renew_id: bool = False) -> DedupDecision:
        if renew_id:
            inc.incident_id = new_incident_id()
            inc.first_seen = now
        inc.last_alerted = now
        inc.alerts_sent += 1
        self.history.append(inc)
        return DedupDecision(True, inc, reason)

    def tick(self) -> list[Incident]:
        """Call every poll (even with no detections). Expires stale pendings and
        returns incidents that have fully expired from dedup memory. Incidents
        whose popup has not been seen for ``resolve_after`` are listed once in
        :attr:`newly_gone` (the durable incident engine resolves on that)."""
        now = self.clock()
        for fp, pend in list(self._pending.items()):
            if now - pend.last_seen > self.resolve_after:
                del self._pending[fp]
        self.newly_gone = []
        for inc in self.active.values():
            if not inc.gone_reported and inc.alerts_sent and now - inc.last_seen >= self.resolve_after:
                inc.gone_reported = True
                self.newly_gone.append(inc)
        resolved = []
        for fp, inc in list(self.active.items()):
            if now - inc.last_seen >= self.resolve_after * 4 + self.cooldown:
                resolved.append(inc)
                del self.active[fp]
        return resolved
