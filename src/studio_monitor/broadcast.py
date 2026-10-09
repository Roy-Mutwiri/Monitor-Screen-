"""Broadcast-state engine: LIVE / NOT_LIVE / UNKNOWN from Studio UI evidence.

"Studio is running" and "the broadcast is live" are different things. This
engine looks only at OCR text from the configured live-status regions (or the
whole main window) of *valid* observations: the main window captured reliably,
no restriction popup detected in the same poll. Evidence is scored by the rules
in ``rules/live_state_rules.json``; a classification only becomes the
*confirmed* state after ``confirm_observations`` consecutive identical
observations with no gap larger than ``max_gap_seconds`` between them.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .detection.rules import normalize_text


class LiveState(str, Enum):
    LIVE = "LIVE"
    NOT_LIVE = "NOT_LIVE"
    UNKNOWN = "UNKNOWN"


@dataclass
class Evidence:
    name: str
    score: int
    detail: str


@dataclass
class Classification:
    state: LiveState
    live_score: int = 0
    not_live_score: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        ev = ", ".join(f"{e.name}({e.detail})" for e in self.evidence) or "no evidence"
        return f"{self.state.value}: {ev}" + (f" [{self.reason}]" if self.reason else "")


def phrase_in(phrase: str, normalized: str) -> bool:
    """Whole-word phrase match on normalized text ("login" must not match
    "logintest", "live" must not match "deliver")."""
    if not phrase:
        return False
    return re.search(r"(?<![a-z0-9'])" + re.escape(phrase) + r"(?![a-z0-9'])", normalized) is not None


@dataclass
class _EvidenceRule:
    name: str
    phrases: list[str]
    regex: Optional[re.Pattern]
    requires_timer: bool
    score: int

    def check(self, normalized: str, has_timer: bool) -> Optional[Evidence]:
        hit = next((p for p in self.phrases if phrase_in(p, normalized)), None)
        detail = hit or ""
        if self.regex is not None:
            m = self.regex.search(normalized)
            if m is None:
                return None
            detail = (detail + " " if detail else "") + m.group(0)
        elif hit is None:
            return None
        if self.requires_timer:
            if not has_timer:
                return None
            detail += " +timer"
        return Evidence(self.name, self.score, detail.strip())


class LiveRules:
    def __init__(self, data: dict) -> None:
        self.verified = bool(data.get("verified", False))
        self.timer_regex = re.compile(data.get("timer_regex") or r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
        self.live_min = int((data.get("live") or {}).get("min_score", 2))
        self.not_live_min = int((data.get("not_live") or {}).get("min_score", 2))
        self.live_rules = self._parse((data.get("live") or {}).get("evidence", []))
        self.not_live_rules = self._parse((data.get("not_live") or {}).get("evidence", []))
        self.unknown_phrases = [normalize_text(p) for p in (data.get("unknown") or {}).get("any", [])]

    @staticmethod
    def _parse(items: list[dict]) -> list[_EvidenceRule]:
        out = []
        for it in items:
            out.append(_EvidenceRule(
                name=str(it.get("name", "rule")),
                phrases=[normalize_text(p) for p in it.get("any", []) if p.strip()],
                regex=re.compile(it["regex"], re.I) if it.get("regex") else None,
                requires_timer=bool(it.get("requires_timer", False)),
                score=int(it.get("score", 1)),
            ))
        return out

    @classmethod
    def load(cls, path: Path) -> "LiveRules":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    CONTROL_RULES = ("end_live_control", "go_live_control")

    def classify_frame(self, text: str, boxes=None, exclude_boxes=None, control_label: str = "") -> Classification:
        """Origin-aware classification of one frame.

        * ``boxes`` (OCR line boxes) + ``exclude_boxes`` (popups, chat panel, title bar, side panels): only text
          outside the excluded regions is evidence, so "Go LIVE" in a chat message, "Lets Go LIVE!" in the title
          chip or "End LIVE?" inside a dialog never count.
        * ``control_label``: the label of the located Go/End LIVE *control* (red button). When known, it is the
          only control evidence; phrase-based control rules are skipped.
        Without geometry this is the plain ``classify``.
        """
        if boxes:
            ex = list(exclude_boxes or [])

            def inside(b) -> bool:
                return any(x <= b.cx <= x2 and y <= b.cy <= y2 for x, y, x2, y2 in ex)
            kept = [b for b in boxes if not inside(b)]
            text = "\n".join(b.text for b in kept)
        norm = normalize_text(text)
        if not norm and not control_label:
            return Classification(LiveState.UNKNOWN, reason="no text")
        for p in self.unknown_phrases:
            if phrase_in(p, norm):
                return Classification(LiveState.UNKNOWN, reason=f"transitional screen: '{p}'")
        has_timer = self.timer_regex.search(text) is not None
        skip = set(self.CONTROL_RULES) if control_label else set()
        live_ev = [e for e in (r.check(norm, has_timer) for r in self.live_rules if r.name not in skip) if e]
        not_live_ev = [e for e in (r.check(norm, has_timer) for r in self.not_live_rules if r.name not in skip) if e]
        if control_label:
            lab = normalize_text(control_label)
            if any(phrase_in(p, lab) for p in ("end live", "end broadcast", "end stream", "stop live", "stop streaming")):
                live_ev.append(Evidence("live_control", 2, f"control reads '{control_label}'"))
            elif any(phrase_in(p, lab) for p in ("go live", "start live", "start broadcast", "start stream", "start streaming")):
                not_live_ev.append(Evidence("live_control", 2, f"control reads '{control_label}'"))
        ls, nls = sum(e.score for e in live_ev), sum(e.score for e in not_live_ev)
        ev = live_ev + not_live_ev
        if ls >= self.live_min and nls < self.not_live_min:
            return Classification(LiveState.LIVE, ls, nls, ev)
        if nls >= self.not_live_min and ls < self.live_min:
            return Classification(LiveState.NOT_LIVE, ls, nls, ev)
        reason = "contradictory evidence" if (ls >= self.live_min and nls >= self.not_live_min) else "insufficient evidence"
        return Classification(LiveState.UNKNOWN, ls, nls, ev, reason)

    CONTROL_RULES = ("end_live_control", "go_live_control")

    def classify_frame(self, text: str, boxes=None, exclude_boxes=None, control_label: str = "") -> Classification:
        """Origin-aware classification of one frame.

        * ``boxes`` (OCR line boxes) + ``exclude_boxes`` (popups, chat panel, title bar, side panels): only text
          outside the excluded regions is evidence, so "Go LIVE" in a chat message, "Lets Go LIVE!" in the title
          chip or "End LIVE?" inside a dialog never count.
        * ``control_label``: the label of the located Go/End LIVE *control* (red button). When known, it is the
          only control evidence; phrase-based control rules are skipped.
        Without geometry this is the plain ``classify``.
        """
        if boxes:
            ex = list(exclude_boxes or [])

            def inside(b) -> bool:
                return any(x <= b.cx <= x2 and y <= b.cy <= y2 for x, y, x2, y2 in ex)
            kept = [b for b in boxes if not inside(b)]
            text = "\n".join(b.text for b in kept)
        norm = normalize_text(text)
        if not norm and not control_label:
            return Classification(LiveState.UNKNOWN, reason="no text")
        for p in self.unknown_phrases:
            if phrase_in(p, norm):
                return Classification(LiveState.UNKNOWN, reason=f"transitional screen: '{p}'")
        has_timer = self.timer_regex.search(text) is not None
        skip = set(self.CONTROL_RULES) if control_label else set()
        live_ev = [e for e in (r.check(norm, has_timer) for r in self.live_rules if r.name not in skip) if e]
        not_live_ev = [e for e in (r.check(norm, has_timer) for r in self.not_live_rules if r.name not in skip) if e]
        if control_label:
            lab = normalize_text(control_label)
            if any(phrase_in(p, lab) for p in ("end live", "end broadcast", "end stream", "stop live", "stop streaming")):
                live_ev.append(Evidence("live_control", 2, f"control reads '{control_label}'"))
            elif any(phrase_in(p, lab) for p in ("go live", "start live", "start broadcast", "start stream", "start streaming")):
                not_live_ev.append(Evidence("live_control", 2, f"control reads '{control_label}'"))
        ls, nls = sum(e.score for e in live_ev), sum(e.score for e in not_live_ev)
        ev = live_ev + not_live_ev
        if ls >= self.live_min and nls < self.not_live_min:
            return Classification(LiveState.LIVE, ls, nls, ev)
        if nls >= self.not_live_min and ls < self.live_min:
            return Classification(LiveState.NOT_LIVE, ls, nls, ev)
        reason = "contradictory evidence" if (ls >= self.live_min and nls >= self.not_live_min) else "insufficient evidence"
        return Classification(LiveState.UNKNOWN, ls, nls, ev, reason)

    def classify(self, text: str) -> Classification:
        """Classify one OCR observation. Never trusts a bare 'LIVE' word."""
        norm = normalize_text(text)
        if not norm:
            return Classification(LiveState.UNKNOWN, reason="no text")
        for p in self.unknown_phrases:
            if phrase_in(p, norm):
                return Classification(LiveState.UNKNOWN, reason=f"transitional screen: '{p}'")
        has_timer = self.timer_regex.search(text) is not None
        live_ev = [e for e in (r.check(norm, has_timer) for r in self.live_rules) if e]
        not_live_ev = [e for e in (r.check(norm, has_timer) for r in self.not_live_rules) if e]
        ls, nls = sum(e.score for e in live_ev), sum(e.score for e in not_live_ev)
        ev = live_ev + not_live_ev
        if ls >= self.live_min and nls < self.not_live_min:
            return Classification(LiveState.LIVE, ls, nls, ev)
        if nls >= self.not_live_min and ls < self.live_min:
            return Classification(LiveState.NOT_LIVE, ls, nls, ev)
        reason = "contradictory evidence" if (ls >= self.live_min and nls >= self.not_live_min) else "insufficient evidence"
        return Classification(LiveState.UNKNOWN, ls, nls, ev, reason)


@dataclass
class ConfirmedState:
    state: LiveState = LiveState.UNKNOWN
    since_mono: Optional[float] = None
    since_utc: str = ""
    last_confirmed_mono: Optional[float] = None
    last_confirmed_utc: str = ""
    evidence: str = ""
    candidate: Optional[LiveState] = None
    candidate_count: int = 0
    fresh: bool = False   # True when the latest observation itself confirmed ``state``
    transitions: list[tuple[LiveState, LiveState, str]] = field(default_factory=list)


class BroadcastStateEngine:
    def __init__(self, rules: LiveRules, confirm_observations: int = 3, max_gap_seconds: float = 30.0,
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.rules = rules
        self.confirm_n = max(1, confirm_observations)
        self.max_gap = max_gap_seconds
        self.clock = clock
        self.mono = mono
        self.state = ConfirmedState()
        self._last_obs_mono: Optional[float] = None
        self.last_classification: Optional[Classification] = None
        self.paused = False   # identity lookup interval: the profile menu may hide live indicators

    def observe(self, classification: Optional[Classification]) -> ConfirmedState:
        """Feed one poll. ``None`` means no valid observation this poll
        (degraded capture, popup on screen, Studio not running). While
        ``paused`` the confirmed state is frozen: no transition, no fresh
        confirmation, no streak change."""
        now = self.mono()
        st = self.state
        if self.paused:
            st.fresh = False
            return st
        if classification is None:
            classification = Classification(LiveState.UNKNOWN, reason="no valid observation")
        self.last_classification = classification
        gap_ok = self._last_obs_mono is not None and (now - self._last_obs_mono) <= self.max_gap
        self._last_obs_mono = now
        obs = classification.state
        if obs == st.candidate and gap_ok:
            st.candidate_count += 1
        else:
            st.candidate, st.candidate_count = obs, 1
        if st.candidate_count >= self.confirm_n and obs != st.state:
            st.transitions.append((st.state, obs, classification.summary()))
            st.state = obs
            st.since_mono = now
            st.since_utc = _utc(self.clock())
        st.fresh = obs == st.state and st.candidate_count >= self.confirm_n
        if st.fresh:
            st.last_confirmed_mono = now
            st.last_confirmed_utc = _utc(self.clock())
            st.evidence = classification.summary()
        return st

    def drain_transitions(self) -> list[tuple[LiveState, LiveState, str]]:
        t, self.state.transitions = self.state.transitions, []
        return t

    @property
    def confirmed(self) -> LiveState:
        return self.state.state


def _utc(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
