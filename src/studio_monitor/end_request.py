"""End-LIVE confirmation ("End streaming?") detection and end-request episodes.

The dialog is detected from spatially related OCR evidence (heading + "End
now" button + body/Cancel within a few consecutive lines), confirmed over a
short window of fresh frames, and tracked as its own episode: opening it
never changes the broadcast state. Outcomes are decided only by evidence
the monitor already trusts: the live-state engine confirming NOT_LIVE
(ended), a fresh LIVE confirmation after the dialog disappeared
(continued), or Studio exiting / no valid capture inside a bounded window
(unknown). Invalid capture never counts as the dialog disappearing.

Episodes are persisted (kv_state) so a restart neither re-alerts for a
dialog that is still open nor forgets one whose outcome is pending; the
persisted episode is reconciled with fresh frames before it is updated.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .detection.rules import normalize_text
from .broadcast import phrase_in

STATE_KEY = "end_request_episode"
OUTCOME_ENDED, OUTCOME_CONTINUED, OUTCOME_UNKNOWN = "ended", "continued", "unknown"


class EndDialogRules:
    def __init__(self, data: dict) -> None:
        g = lambda k: [normalize_text(p) for p in data.get(k, [])]
        self.verified = bool(data.get("verified", False))
        self.heading, self.body, self.confirm, self.cancel = g("heading"), g("body"), g("confirm_button"), g("cancel_button")
        self.max_line_span = int(data.get("max_line_span", 6))
        self.max_char_span = int(data.get("max_char_span", 220))
        self.confirm_frames = int(data.get("confirm_frames", 2))
        self.confirm_window = float(data.get("confirm_window_seconds", 12))
        self.closed_after_frames = int(data.get("closed_after_frames", 2))
        self.resolve_timeout = float(data.get("resolve_timeout_seconds", 180))

    @classmethod
    def load(cls, path: Path) -> "EndDialogRules":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------
    def match(self, text: str, lines: Optional[list[str]] = None) -> Optional[dict]:
        """Return the matched parts when the heading, the confirm button and the body/cancel are spatially related."""
        lines = [ln for ln in (lines or []) if ln and ln.strip()]
        if len(lines) < 2:
            lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if len(lines) >= 2:
            return self._match_lines(lines)
        return self._match_span(text or "")

    def _parts(self, norm: str) -> dict:
        return {"heading": next((p for p in self.heading if phrase_in(p, norm)), None),
                "body": next((p for p in self.body if phrase_in(p, norm)), None),
                "confirm": next((p for p in self.confirm if phrase_in(p, norm)), None),
                "cancel": next((p for p in self.cancel if phrase_in(p, norm)), None)}

    def _match_lines(self, lines: list[str]) -> Optional[dict]:
        norms = [normalize_text(ln) for ln in lines]
        for i, n in enumerate(norms):
            head = next((p for p in self.heading if phrase_in(p, n)), None)
            if head is None:
                continue
            window = norms[i:i + self.max_line_span + 1]
            joined = " \n ".join(window)
            parts = self._parts(joined)
            parts["heading"] = head
            if parts["confirm"] and (parts["body"] or parts["cancel"]):
                parts["evidence"] = " | ".join(lines[i:i + self.max_line_span + 1])[:300]
                return parts
        return None

    def _match_span(self, text: str) -> Optional[dict]:
        norm = normalize_text(text)
        for head in self.heading:
            idx = norm.find(head)
            if idx < 0:
                continue
            window = norm[idx: idx + self.max_char_span]
            parts = self._parts(window)
            parts["heading"] = head
            if parts["confirm"] and (parts["body"] or parts["cancel"]):
                parts["evidence"] = text[max(0, idx - 5): idx + self.max_char_span][:300]
                return parts
        return None


@dataclass
class EndRequestEpisode:
    episode_id: str
    broadcast_episode: str = ""
    session_id: str = ""
    opened_at: float = 0.0            # wall clock of the first matching frame
    opened_utc: str = ""
    last_seen_at: float = 0.0
    closed_at: float = 0.0            # dialog no longer visible on fresh frames
    alerted: bool = False
    event_id: str = ""
    incident_id: str = ""
    evidence_path: str = ""
    evidence_text: str = ""
    outcome: str = ""                 # "" | ended | continued | unknown
    outcome_at: float = 0.0
    outcome_utc: str = ""
    outcome_reason: str = ""
    confirmed_end_utc: str = ""
    reconciling: bool = False         # loaded from persistence after a restart; needs fresh evidence first
    miss_count: int = 0

    @property
    def open(self) -> bool:
        return not self.outcome

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EndRequestEpisode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class EndRequestEvent:
    kind: str                         # opened | ended | continued | unknown
    episode: EndRequestEpisode
    view_index: int = -1              # which OCR view matched (for the evidence capture)
    detail: str = ""


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class EndRequestTracker:
    def __init__(self, rules: EndDialogRules, get_state: Callable, set_state: Callable, clock: Callable[[], float] = time.time,
                 restart_grace: Optional[float] = None) -> None:
        self.rules = rules
        self.get_state, self.set_state = get_state, set_state
        self.clock = clock
        self.current: Optional[EndRequestEpisode] = None
        self.last: Optional[EndRequestEpisode] = None
        self.history: list[EndRequestEpisode] = []
        self._pending = 0
        self._pending_since = 0.0
        self._restart_grace = rules.resolve_timeout if restart_grace is None else restart_grace
        self._restarted_at = clock()
        saved = get_state(STATE_KEY, None)
        if saved:
            ep = EndRequestEpisode.from_dict(saved)
            if ep.open:
                ep.reconciling = True
                ep.miss_count = 0
                self.current = ep
            else:
                self.last = ep

    # ------------------------------------------------------------------
    @property
    def dialog_open(self) -> bool:
        return self.current is not None and self.current.closed_at == 0.0

    def _persist(self) -> None:
        ep = self.current or self.last
        if ep is not None:
            self.set_state(STATE_KEY, ep.to_dict())

    def _finish(self, outcome: str, reason: str, now: float) -> EndRequestEvent:
        ep = self.current
        ep.outcome, ep.outcome_at, ep.outcome_utc, ep.outcome_reason = outcome, now, _utc(now), reason
        if outcome == OUTCOME_ENDED:
            ep.confirmed_end_utc = _utc(now)
        self.last, self.current = ep, None
        self.history.append(ep)
        self._persist()
        return EndRequestEvent(outcome, ep, detail=reason)

    # ------------------------------------------------------------------
    def observe(self, views: list[tuple[str, list[str]]], fresh: bool, live_state: str, live_fresh: bool,
                studio_running: bool, broadcast_episode: str = "", session_id: str = "", strong: bool = False) -> list[EndRequestEvent]:
        """One poll. ``views`` are (text, lines) of every OCR'd capture this poll; ``fresh`` says whether they are
        fresh valid frames. ``live_state``/``live_fresh`` come from the live-state engine after this poll.
        ``strong``: the dialog was read with geometry as a credible panel (heading + End now + Cancel): one frame
        confirms it, because the operator can dismiss the dialog within a second (real sessions 2026-10-09)."""
        now = self.clock()
        events: list[EndRequestEvent] = []
        matched_idx = -1
        parts = None
        if fresh:
            for i, (text, lines) in enumerate(views):
                parts = self.rules.match(text, lines)
                if parts:
                    matched_idx = i
                    break
        seen = fresh and matched_idx >= 0

        ep = self.current
        if ep is None:
            if seen:
                if self._pending and now - self._pending_since > self.rules.confirm_window:
                    self._pending = 0
                if self._pending == 0:
                    self._pending_since = now
                self._pending += 1
                if self._pending >= (1 if strong else self.rules.confirm_frames):
                    ep = EndRequestEpisode(episode_id=f"END-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}",
                                           broadcast_episode=broadcast_episode, session_id=session_id,
                                           opened_at=self._pending_since, opened_utc=_utc(self._pending_since), last_seen_at=now,
                                           evidence_text=(parts or {}).get("evidence", ""))
                    self.current = ep
                    self._pending = 0
                    self._persist()
                    events.append(EndRequestEvent("opened", ep, matched_idx, (parts or {}).get("evidence", "")))
            elif fresh:
                self._pending = 0
            return events

        # ---- an episode is open ------------------------------------------------
        if seen:
            ep.last_seen_at, ep.miss_count, ep.reconciling = now, 0, False
            if ep.closed_at:                       # it came back before an outcome was decided: same request, still open
                ep.closed_at = 0.0
        elif fresh:
            ep.reconciling = False
            ep.miss_count += 1
            if ep.miss_count >= self.rules.closed_after_frames and not ep.closed_at:
                ep.closed_at = now
        # outcome evaluation (only on real evidence)
        if live_fresh and live_state == "NOT_LIVE":
            events.append(self._finish(OUTCOME_ENDED, "broadcast confirmed NOT_LIVE after the end dialog", now))
        elif ep.closed_at and live_fresh and live_state == "LIVE" and not seen:
            events.append(self._finish(OUTCOME_CONTINUED, "dialog no longer visible; fresh evidence confirms LIVE continues", now))
        elif not studio_running:
            events.append(self._finish(OUTCOME_UNKNOWN, "Studio exited while the end dialog outcome was pending", now))
        else:
            anchor = ep.closed_at or (self._restarted_at if ep.reconciling else ep.last_seen_at or ep.opened_at)
            if not seen and now - anchor > self.rules.resolve_timeout:
                why = "no valid capture after monitor restart" if ep.reconciling else \
                    ("no fresh evidence of LIVE or NOT_LIVE after the dialog disappeared" if ep.closed_at else
                     "no valid capture for too long while the dialog was open")
                events.append(self._finish(OUTCOME_UNKNOWN, why, now))
        if self.current is not None:
            self._persist()
        return events

    def note_broadcast_ended(self, now: Optional[float] = None) -> Optional[EndRequestEvent]:
        """Called by the broadcast engine path when an episode ends (confirmed NOT_LIVE)."""
        if self.current is None:
            return None
        return self._finish(OUTCOME_ENDED, "broadcast confirmed NOT_LIVE after the end dialog", now if now is not None else self.clock())

    def mark_alerted(self, event_id: str, incident_id: str, evidence_path: str) -> None:
        if self.current is not None:
            self.current.alerted, self.current.event_id, self.current.incident_id = True, event_id, incident_id
            self.current.evidence_path = evidence_path
            self._persist()

    def report_line(self, broadcast_episode: str = "", session_id: str = "") -> str:
        eps = [e for e in self.history + ([self.current] if self.current else [])
               if (broadcast_episode and e.broadcast_episode == broadcast_episode) or (session_id and e.session_id == session_id)]
        if not eps:
            return ""
        parts = []
        for e in eps:
            o = e.outcome or "pending"
            parts.append(f"end dialog at {e.opened_utc[11:19]} UTC -> {o}" + (f" ({e.outcome_reason})" if e.outcome == OUTCOME_UNKNOWN else ""))
        return "End-request dialog: " + "; ".join(parts)
