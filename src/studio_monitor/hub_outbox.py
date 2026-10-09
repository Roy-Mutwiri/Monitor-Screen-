"""Durable agent→hub outbox (SQLite, same database file as the delivery queue).

Events are stored as contract JSON when dispatched and uploaded in batches
with exponential backoff; accepted/duplicate events move to ``done`` (after an
optional evidence upload), rejected ones are kept for inspection. Offline
periods therefore lose nothing; the hub deduplicates by ``event_id``.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_outbox (
    event_id TEXT PRIMARY KEY,
    event_json TEXT NOT NULL,
    evidence_path TEXT NOT NULL DEFAULT '',
    evidence_sha256 TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | evidence | done | rejected
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    uploaded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_hub_outbox_due ON hub_outbox(status, next_attempt);
"""


@dataclass
class OutboxItem:
    event_id: str
    event: dict
    evidence_path: str
    evidence_sha256: str
    status: str
    attempts: int
    last_error: str


class HubOutbox:
    def __init__(self, db_path: Path | str, clock: Callable[[], float] = time.time, backoff_base: float = 5.0,
                 backoff_max: float = 300.0, retention_seconds: float = 7 * 86400) -> None:
        self.clock = clock
        self.backoff_base, self.backoff_max, self.retention = backoff_base, backoff_max, retention_seconds
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    def enqueue(self, event: dict, evidence_path: str = "", evidence_sha256: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO hub_outbox(event_id, event_json, evidence_path, evidence_sha256, created_at, next_attempt) "
                "VALUES (?,?,?,?,?,?)", (event["event_id"], json.dumps(event, ensure_ascii=False), evidence_path or "",
                                       evidence_sha256 or "", self.clock(), 0))
            self._conn.commit()
            return cur.rowcount == 1

    def due(self, limit: int = 50) -> list[OutboxItem]:
        now = self.clock()
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_json, evidence_path, evidence_sha256, status, attempts, last_error FROM hub_outbox "
                "WHERE status='pending' AND next_attempt<=? ORDER BY created_at LIMIT ?", (now, limit)).fetchall()
        return [OutboxItem(r[0], json.loads(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in rows]

    def evidence_due(self, limit: int = 5) -> list[OutboxItem]:
        now = self.clock()
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_json, evidence_path, evidence_sha256, status, attempts, last_error FROM hub_outbox "
                "WHERE status='evidence' AND next_attempt<=? ORDER BY created_at LIMIT ?", (now, limit)).fetchall()
        return [OutboxItem(r[0], json.loads(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in rows]

    def mark_accepted(self, event_id: str, needs_evidence: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE hub_outbox SET status=?, uploaded_at=?, last_error='', next_attempt=0 WHERE event_id=?",
                               ("evidence" if needs_evidence else "done", self.clock(), event_id))
            self._conn.commit()

    def mark_done(self, event_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE hub_outbox SET status='done', last_error='' WHERE event_id=?", (event_id,))
            self._conn.commit()

    def mark_rejected(self, event_id: str, reason: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE hub_outbox SET status='rejected', last_error=? WHERE event_id=?", (reason[:500], event_id))
            self._conn.commit()

    def mark_failed(self, event_ids: list[str], error: str) -> float:
        """Exponential backoff shared by the batch; returns the delay applied."""
        if not event_ids:
            return 0.0
        with self._lock:
            attempts = self._conn.execute("SELECT MAX(attempts) FROM hub_outbox WHERE event_id IN (%s)" %
                                          ",".join("?" * len(event_ids)), event_ids).fetchone()[0] or 0
            delay = min(self.backoff_max, self.backoff_base * (2 ** attempts))
            self._conn.execute("UPDATE hub_outbox SET attempts=attempts+1, next_attempt=?, last_error=? WHERE event_id IN (%s)" %
                               ",".join("?" * len(event_ids)), [self.clock() + delay, error[:500], *event_ids])
            self._conn.commit()
        return delay

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM hub_outbox GROUP BY status").fetchall()
        c = {"pending": 0, "evidence": 0, "done": 0, "rejected": 0}
        c.update({r[0]: r[1] for r in rows})
        return c

    def last_error(self) -> str:
        with self._lock:
            row = self._conn.execute("SELECT last_error FROM hub_outbox WHERE last_error<>'' ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else ""

    def purge(self) -> int:
        cutoff = self.clock() - self.retention
        with self._lock:
            cur = self._conn.execute("DELETE FROM hub_outbox WHERE status IN ('done','rejected') AND created_at<?", (cutoff,))
            self._conn.commit()
        return cur.rowcount
