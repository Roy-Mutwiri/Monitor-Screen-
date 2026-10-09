"""Durable incident engine: one incident per device / session / category /
problem episode, with lifecycle (OPEN / RESOLVED), acknowledgement
(separate from lifecycle), scoped snooze / maintenance suppression, severity
(INFO / WARNING / URGENT), escalation reminders, per-destination root
message ids for threaded Telegram replies, coalesced updates and a timeline.

Acknowledgement pauses escalation; it never resolves the fault. Resolution
text must match the evidence (e.g. "verification prompt no longer visible"
does not claim the restriction was lifted). All state changes are SQLite
transactions; escalation sends are claimed atomically so a concurrent
acknowledgement/resolution cannot race a reminder.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .contracts.events import Severity

OPEN, RESOLVED = "OPEN", "RESOLVED"

# default escalation policy per severity: (first reminder delay s, interval s, max reminders)
DEFAULT_ESCALATION = {
    Severity.URGENT: (300.0, 300.0, 3),
    Severity.WARNING: (1800.0, 1800.0, 2),
    Severity.INFO: (0.0, 0.0, 0),
}
# categories that maintenance/snooze never silence unless explicitly chosen
CRITICAL_CATEGORIES = ("restrictions", "verification")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents_v2 (
    incident_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    problem_key TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    summary TEXT NOT NULL,
    opened_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    resolved_utc TEXT,
    resolution TEXT NOT NULL DEFAULT '',
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acknowledged_by TEXT NOT NULL DEFAULT '',
    acknowledged_utc TEXT,
    ack_note TEXT NOT NULL DEFAULT '',
    evidence_path TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    owner_label TEXT NOT NULL DEFAULT '',
    occurrences INTEGER NOT NULL DEFAULT 1,
    reminders_sent INTEGER NOT NULL DEFAULT 0,
    next_reminder_at REAL,
    escalation_seq INTEGER NOT NULL DEFAULT 0,
    opened_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inc2_open ON incidents_v2(device_id, status, category, problem_key);
CREATE TABLE IF NOT EXISTS incident_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    ts_utc TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    coalesced INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_messages (
    incident_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id INTEGER,
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (incident_id, bot_id, chat_id)
);
CREATE TABLE IF NOT EXISTS suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,          -- incident | device | category
    key TEXT NOT NULL,            -- incident_id | device_id | device_id:category
    until_at REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance (
    device_id TEXT PRIMARY KEY,
    until_at REAL NOT NULL,
    categories TEXT NOT NULL,     -- JSON list of suppressed categories
    reason TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL
);
"""


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Incident:
    incident_id: str
    device_id: str
    session_id: str
    category: str
    problem_key: str
    severity: str
    status: str
    summary: str
    opened_utc: str
    updated_utc: str
    resolved_utc: Optional[str]
    resolution: str
    acknowledged: bool
    acknowledged_by: str
    acknowledged_utc: Optional[str]
    ack_note: str
    evidence_path: str
    account: str
    owner_label: str
    occurrences: int
    reminders_sent: int
    next_reminder_at: Optional[float]
    escalation_seq: int
    opened_at: float

    @property
    def is_open(self) -> bool:
        return self.status == OPEN


@dataclass
class EscalationDue:
    incident: Incident
    sequence: int          # reminder number (1-based)
    claim_seq: int         # escalation_seq to pass to claim_escalation


@dataclass
class IncidentChange:
    incident: Incident
    is_new: bool
    coalesced: bool = False


class IncidentEngine:
    def __init__(self, conn: sqlite3.Connection, lock: Optional[threading.RLock] = None,
                 clock: Callable[[], float] = time.time, escalation: Optional[dict] = None,
                 coalesce_seconds: float = 120.0) -> None:
        self.conn = conn
        self.lock = lock or threading.RLock()
        self.clock = clock
        self.escalation = dict(DEFAULT_ESCALATION)
        if escalation:
            self.escalation.update(escalation)
        self.coalesce_seconds = coalesce_seconds
        with self.lock:
            conn.executescript(_SCHEMA)

    # ---------------------------------------------------------------- helpers
    _COLS = ("incident_id, device_id, session_id, category, problem_key, severity, status, summary, opened_utc, "
             "updated_utc, resolved_utc, resolution, acknowledged, acknowledged_by, acknowledged_utc, ack_note, "
             "evidence_path, account, owner_label, occurrences, reminders_sent, next_reminder_at, escalation_seq, opened_at")

    def _row(self, r) -> Incident:
        vals = list(r)
        vals[12] = bool(vals[12])
        return Incident(*vals)

    def get(self, incident_id: str) -> Optional[Incident]:
        with self.lock:
            r = self.conn.execute(f"SELECT {self._COLS} FROM incidents_v2 WHERE incident_id=?", (incident_id,)).fetchone()
        return self._row(r) if r else None

    def find_open(self, device_id: str, category: str, problem_key: str, session_id: str = "") -> Optional[Incident]:
        with self.lock:
            r = self.conn.execute(
                f"SELECT {self._COLS} FROM incidents_v2 WHERE device_id=? AND status='OPEN' AND category=? AND problem_key=? "
                "AND (session_id=? OR session_id='' OR ?='') ORDER BY opened_at DESC LIMIT 1",
                (device_id, category, problem_key, session_id, session_id)).fetchone()
        return self._row(r) if r else None

    def list(self, device_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> list[Incident]:
        sql, params = f"SELECT {self._COLS} FROM incidents_v2 WHERE 1=1", []
        if device_id:
            sql += " AND device_id=?"; params.append(device_id)
        if status:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY opened_at DESC LIMIT ?"; params.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def _timeline(self, incident_id: str, kind: str, text: str, actor: str = "") -> None:
        now = self.clock()
        self.conn.execute("INSERT INTO incident_timeline (incident_id, ts_utc, kind, text, actor, created_at) VALUES (?,?,?,?,?,?)",
                          (incident_id, _utc(now), kind, text[:2000], actor, now))

    def timeline(self, incident_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT ts_utc, kind, text, actor, coalesced FROM incident_timeline WHERE incident_id=? "
                                     "ORDER BY id", (incident_id,)).fetchall()
        return [{"ts_utc": r[0], "kind": r[1], "text": r[2], "actor": r[3], "coalesced": r[4]} for r in rows]

    def _schedule_first_reminder(self, severity: str, now: float) -> Optional[float]:
        delay, _interval, max_n = self.escalation.get(severity, (0.0, 0.0, 0))
        return now + delay if max_n > 0 else None

    # ---------------------------------------------------------------- lifecycle
    def open_or_update(self, device_id: str, category: str, problem_key: str, severity: str, summary: str, *,
                       session_id: str = "", evidence_path: str = "", account: str = "", owner_label: str = "",
                       observed_utc: str = "", incident_id: str = "") -> IncidentChange:
        """Open a new incident or record another occurrence of the open one
        (coalesced within ``coalesce_seconds``). Suppressed categories still
        get incidents (so history is complete); callers decide delivery."""
        now = self.clock()
        observed_utc = observed_utc or _utc(now)
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self.find_open(device_id, category, problem_key, session_id)
                if existing is not None:
                    coalesced = (now - datetime.fromisoformat(existing.updated_utc).timestamp()) < self.coalesce_seconds
                    sev = severity if _rank(severity) > _rank(existing.severity) else existing.severity
                    self.conn.execute(
                        "UPDATE incidents_v2 SET occurrences=occurrences+1, updated_utc=?, severity=?, "
                        "evidence_path=CASE WHEN ?<>'' THEN ? ELSE evidence_path END, summary=? WHERE incident_id=?",
                        (observed_utc, sev, evidence_path, evidence_path, summary[:1000], existing.incident_id))
                    if coalesced:
                        # fold frequent repeats into the latest opened/update entry instead of spamming the timeline
                        self.conn.execute(
                            "UPDATE incident_timeline SET coalesced=coalesced+1, text=? WHERE id=(SELECT MAX(id) FROM incident_timeline "
                            "WHERE incident_id=? AND kind IN ('opened','update'))", (summary[:2000], existing.incident_id))
                        if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
                            self._timeline(existing.incident_id, "update", summary)
                    else:
                        self._timeline(existing.incident_id, "update", summary)
                    self.conn.execute("COMMIT")
                    return IncidentChange(self.get(existing.incident_id), False, coalesced)
                incident_id = incident_id or f"INC-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"
                if self.get(incident_id) is not None:
                    incident_id = f"{incident_id}-{secrets.token_hex(2).upper()}"
                self.conn.execute(
                    f"INSERT INTO incidents_v2 ({self._COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (incident_id, device_id, session_id, category, problem_key, severity, OPEN, summary[:1000], observed_utc,
                     observed_utc, None, "", 0, "", None, "", evidence_path, account, owner_label, 1, 0,
                     self._schedule_first_reminder(severity, now), 0, now))
                self._timeline(incident_id, "opened", summary)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return IncidentChange(self.get(incident_id), True)

    def resolve(self, incident_id: str, resolution: str, actor: str = "system", observed_utc: str = "") -> Optional[Incident]:
        """Close the lifecycle. ``resolution`` must describe the evidence (what
        is no longer visible), not an outcome the detector cannot know."""
        now = self.clock()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "UPDATE incidents_v2 SET status='RESOLVED', resolved_utc=?, resolution=?, updated_utc=?, next_reminder_at=NULL, "
                    "escalation_seq=escalation_seq+1 WHERE incident_id=? AND status='OPEN'",
                    (observed_utc or _utc(now), resolution[:1000], _utc(now), incident_id))
                if cur.rowcount:
                    self._timeline(incident_id, "resolved", resolution, actor)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get(incident_id)

    def acknowledge(self, incident_id: str, actor: str, note: str = "") -> Optional[Incident]:
        """Pause escalation; the fault stays OPEN."""
        now = self.clock()
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "UPDATE incidents_v2 SET acknowledged=1, acknowledged_by=?, acknowledged_utc=?, ack_note=?, updated_utc=?, "
                    "next_reminder_at=NULL, escalation_seq=escalation_seq+1 WHERE incident_id=? AND acknowledged=0",
                    (actor[:120], _utc(now), note[:500], _utc(now), incident_id))
                if cur.rowcount:
                    self._timeline(incident_id, "acknowledged", note or "acknowledged", actor)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return self.get(incident_id)

    # ---------------------------------------------------------------- suppression / snooze / maintenance
    def snooze(self, scope: str, key: str, seconds: float, actor: str = "", reason: str = "") -> float:
        assert scope in ("incident", "device", "category")
        now = self.clock()
        until = now + max(60.0, seconds)
        with self.lock:
            self.conn.execute("INSERT INTO suppressions (scope, key, until_at, reason, actor, created_at) VALUES (?,?,?,?,?,?)",
                              (scope, key, until, reason[:300], actor[:120], now))
            if scope == "incident":
                self.conn.execute("UPDATE incidents_v2 SET next_reminder_at=?, escalation_seq=escalation_seq+1 WHERE incident_id=? "
                                  "AND status='OPEN'", (until, key))
                self._timeline(key, "snoozed", f"snoozed until {_utc(until)}" + (f": {reason}" if reason else ""), actor)
        return until

    def is_snoozed(self, device_id: str, category: str = "", incident_id: str = "") -> bool:
        now = self.clock()
        with self.lock:
            keys = [("device", device_id)]
            if category:
                keys.append(("category", f"{device_id}:{category}"))
            if incident_id:
                keys.append(("incident", incident_id))
            for scope, key in keys:
                r = self.conn.execute("SELECT 1 FROM suppressions WHERE scope=? AND key=? AND until_at>? LIMIT 1",
                                      (scope, key, now)).fetchone()
                if r:
                    return True
        return False

    def enter_maintenance(self, device_id: str, seconds: float, categories: list[str], reason: str = "",
                          keep_critical: bool = True) -> float:
        """Suppress *selected* expected conditions for a bounded time. Critical
        categories stay enabled unless explicitly included and keep_critical is False."""
        now = self.clock()
        cats = [c for c in categories if not (keep_critical and c in CRITICAL_CATEGORIES)]
        until = now + max(60.0, seconds)
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO maintenance (device_id, until_at, categories, reason, started_at) VALUES (?,?,?,?,?)",
                              (device_id, until, json.dumps(cats), reason[:300], now))
        return until

    def exit_maintenance(self, device_id: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM maintenance WHERE device_id=?", (device_id,))
            return cur.rowcount > 0

    def maintenance(self, device_id: str) -> Optional[dict]:
        now = self.clock()
        with self.lock:
            r = self.conn.execute("SELECT until_at, categories, reason, started_at FROM maintenance WHERE device_id=?",
                                  (device_id,)).fetchone()
        if not r or r[0] <= now:
            return None
        return {"until_at": r[0], "remaining_seconds": r[0] - now, "categories": json.loads(r[1]), "reason": r[2],
                "started_at": r[3]}

    def is_suppressed(self, device_id: str, category: str, incident_id: str = "") -> bool:
        """Delivery suppression (maintenance or snooze). Incidents are still
        recorded; only notifications are held."""
        m = self.maintenance(device_id)
        if m and category in m["categories"]:
            return True
        return self.is_snoozed(device_id, category, incident_id)

    # ---------------------------------------------------------------- escalation
    def escalations_due(self, limit: int = 20) -> list[EscalationDue]:
        now = self.clock()
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._COLS} FROM incidents_v2 WHERE status='OPEN' AND acknowledged=0 AND next_reminder_at IS NOT NULL "
                "AND next_reminder_at<=? ORDER BY next_reminder_at LIMIT ?", (now, limit)).fetchall()
        out = []
        for r in rows:
            inc = self._row(r)
            if self.is_snoozed(inc.device_id, inc.category, inc.incident_id):
                continue
            out.append(EscalationDue(inc, inc.reminders_sent + 1, inc.escalation_seq))
        return out

    def claim_escalation(self, incident_id: str, claim_seq: int) -> Optional[Incident]:
        """Atomically claim a reminder send. Returns the incident if the claim
        won (no ack/resolve/snooze happened since ``escalations_due``), else None."""
        now = self.clock()
        with self.lock:
            inc = self.get(incident_id)
            if inc is None or not inc.is_open or inc.acknowledged:
                return None
            _delay, interval, max_n = self.escalation.get(inc.severity, (0.0, 0.0, 0))
            next_n = inc.reminders_sent + 1
            next_at = now + interval if next_n < max_n else None
            cur = self.conn.execute(
                "UPDATE incidents_v2 SET reminders_sent=reminders_sent+1, next_reminder_at=?, escalation_seq=escalation_seq+1, "
                "updated_utc=? WHERE incident_id=? AND status='OPEN' AND acknowledged=0 AND escalation_seq=?",
                (next_at, _utc(now), incident_id, claim_seq))
            if cur.rowcount != 1:
                return None
            self._timeline(incident_id, "reminder", f"reminder {next_n} of {max_n}")
        return self.get(incident_id)

    # ---------------------------------------------------------------- telegram threading
    def set_root_message(self, incident_id: str, bot_id: str, chat_id: str, thread_id: Optional[int], message_id: int) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO incident_messages VALUES (?,?,?,?,?,?)",
                              (incident_id, bot_id, chat_id, thread_id, message_id, self.clock()))

    def root_message(self, incident_id: str, bot_id: str, chat_id: str) -> Optional[int]:
        with self.lock:
            r = self.conn.execute("SELECT message_id FROM incident_messages WHERE incident_id=? AND bot_id=? AND chat_id=?",
                                  (incident_id, bot_id, chat_id)).fetchone()
        return int(r[0]) if r else None

    # ---------------------------------------------------------------- summaries
    def session_summary(self, device_id: str, session_id: str) -> dict:
        """Counts/durations computed from the database (never inferred)."""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._COLS} FROM incidents_v2 WHERE device_id=? AND session_id=? ORDER BY opened_at", (device_id, session_id)).fetchall()
        incs = [self._row(r) for r in rows]
        by_cat: dict[str, dict] = {}
        for inc in incs:
            b = by_cat.setdefault(inc.category, {"count": 0, "open": 0, "resolved": 0, "occurrences": 0, "total_seconds": 0.0, "incident_ids": []})
            b["count"] += 1
            b["occurrences"] += inc.occurrences
            b["incident_ids"].append(inc.incident_id)
            if inc.status == OPEN:
                b["open"] += 1
            else:
                b["resolved"] += 1
                if inc.resolved_utc:
                    b["total_seconds"] += max(0.0, datetime.fromisoformat(inc.resolved_utc).timestamp()
                                              - datetime.fromisoformat(inc.opened_utc).timestamp())
        return {"device_id": device_id, "session_id": session_id, "incidents": len(incs), "by_category": by_cat}


def _rank(sev: str) -> int:
    return {Severity.INFO: 0, Severity.WARNING: 1, Severity.URGENT: 2}.get(sev, 0)
