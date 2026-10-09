"""Persistent (SQLite) delivery outbox with retry/backoff, plus local history
tables (restriction incidents, Studio activity events) and a small key/value
state store that can be updated in the same transaction as an enqueue.

Alerts are written here *before* any network call, so a crash, a reboot or a
long Telegram outage never loses an alert. A worker thread drains the queue.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger(__name__)

KIND_INCIDENT = "incident"
KIND_ACTIVITY = "activity"
KIND_REMINDER = "reminder"
KIND_STATUS = "status"


@dataclass
class QueuedAlert:
    id: int
    incident_id: str
    payload: dict
    screenshot_path: str
    attempts: int
    status: str
    last_error: str
    kind: str = KIND_INCIDENT


class DeliveryError(Exception):
    """Transient failure; the item will be retried. ``retry_after`` is a hint."""

    def __init__(self, message: str, retry_after: Optional[float] = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    screenshot_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    sent_at REAL,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_status_next ON alerts(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    detected_text TEXT NOT NULL,
    window_title TEXT NOT NULL DEFAULT '',
    is_dialog INTEGER NOT NULL DEFAULT 0,
    screenshot_path TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    episode_id TEXT NOT NULL DEFAULT '',
    ts_utc TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    screenshot_path TEXT NOT NULL DEFAULT '',
    alert_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class DeliveryQueue:
    def __init__(self, db_path: Path, max_attempts: int = 8, backoff_base: float = 2.0,
                 backoff_max: float = 300.0, clock: Callable[[], float] = time.time) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
        if "kind" not in cols:
            self._conn.execute(f"ALTER TABLE alerts ADD COLUMN kind TEXT NOT NULL DEFAULT '{KIND_INCIDENT}'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes atomically (e.g. enqueue + state update)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # -- key/value state --------------------------------------------------
    def get_state(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv_state(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), self.clock()),
            )

    # -- incidents (restriction evidence history) -------------------------
    def record_incident(self, incident_id: str, category: str, label: str, text: str,
                        window_title: str, is_dialog: bool, screenshot_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                (incident_id, category, label, text, window_title, int(is_dialog), screenshot_path, self.clock()),
            )

    def recent_incidents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT incident_id, category, label, detected_text, window_title, is_dialog, "
                "screenshot_path, created_at FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        keys = ["incident_id", "category", "label", "detected_text", "window_title", "is_dialog",
                "screenshot_path", "created_at"]
        return [dict(zip(keys, r)) for r in rows]

    # -- activity events (Studio opened/closed, reminders) ----------------
    def record_event(self, event_id: str, event_type: str, ts_utc: str, details: Optional[dict] = None,
                     session_id: str = "", episode_id: str = "", screenshot_path: str = "",
                     alert_id: Optional[int] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO activity_events VALUES (?,?,?,?,?,?,?,?,?)",
                (event_id, event_type, session_id, episode_id, ts_utc, json.dumps(details or {}),
                 screenshot_path, alert_id, self.clock()),
            )

    def recent_events(self, limit: int = 50, event_types: Optional[list[str]] = None) -> list[dict]:
        sql = ("SELECT event_id, event_type, session_id, episode_id, ts_utc, details, screenshot_path, "
               "alert_id, created_at FROM activity_events")
        params: list = []
        if event_types:
            sql += " WHERE event_type IN (%s)" % ",".join("?" * len(event_types))
            params += event_types
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        keys = ["event_id", "event_type", "session_id", "episode_id", "ts_utc", "details", "screenshot_path",
                "alert_id", "created_at"]
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            d["details"] = json.loads(d["details"] or "{}")
            out.append(d)
        return out

    def history(self, limit: int = 50, kind: str = "all") -> list[dict]:
        """Unified history for the GUI: kind = all | incident | activity."""
        items = []
        if kind in ("all", "incident"):
            for i in self.recent_incidents(limit):
                items.append({"ts": i["created_at"], "kind": KIND_INCIDENT, "id": i["incident_id"],
                              "label": i["label"], "detail": i["detected_text"][:120]})
        if kind in ("all", "activity"):
            for e in self.recent_events(limit):
                items.append({"ts": e["created_at"], "kind": KIND_ACTIVITY, "id": e["event_id"],
                              "label": e["event_type"], "detail": e["details"].get("summary", "")})
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items[:limit]

    # -- queue ----------------------------------------------------------
    def enqueue(self, incident_id: str, payload: dict, screenshot_path: str = "",
                kind: str = KIND_INCIDENT) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (incident_id, payload, screenshot_path, created_at, kind) VALUES (?,?,?,?,?)",
                (incident_id, json.dumps(payload), screenshot_path, self.clock(), kind),
            )
            return int(cur.lastrowid)

    def next_due(self) -> Optional[QueuedAlert]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, incident_id, payload, screenshot_path, attempts, status, last_error, kind FROM alerts "
                "WHERE status='pending' AND next_attempt_at <= ? ORDER BY id LIMIT 1", (self.clock(),)
            ).fetchone()
        if row is None:
            return None
        return QueuedAlert(row[0], row[1], json.loads(row[2]), row[3], row[4], row[5], row[6], row[7])

    def alert_status(self, alert_id: int) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT status FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return row[0] if row else None

    def cancel(self, alert_id: int, reason: str) -> bool:
        """Cancel a still-pending alert (e.g. a reminder that became obsolete)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE alerts SET status='cancelled', last_error=? WHERE id=? AND status='pending'",
                (reason[:500], alert_id),
            )
            return cur.rowcount == 1

    def seconds_until_next(self) -> Optional[float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(next_attempt_at) FROM alerts WHERE status='pending'"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return max(0.0, row[0] - self.clock())

    def mark_sent(self, alert_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?",
                (self.clock(), alert_id),
            )

    def mark_failed(self, alert_id: int, error: str, retry_after: Optional[float] = None,
                    permanent: bool = False) -> str:
        """Schedule a retry with exponential backoff, or give up. Returns new status."""
        with self._lock:
            row = self._conn.execute("SELECT attempts FROM alerts WHERE id=?", (alert_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            if permanent or attempts >= self.max_attempts:
                status = "failed"
                next_at = 0.0
            else:
                status = "pending"
                delay = min(self.backoff_max, self.backoff_base * (2 ** (attempts - 1)))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                next_at = self.clock() + delay
            self._conn.execute(
                "UPDATE alerts SET status=?, attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                (status, attempts, next_at, error[:500], alert_id),
            )
            return status

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM alerts GROUP BY status").fetchall()
        out = {"pending": 0, "sent": 0, "failed": 0, "cancelled": 0}
        out.update({r[0]: r[1] for r in rows})
        return out

    def counts_by_kind(self, status: str = "pending") -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) FROM alerts WHERE status=? GROUP BY kind", (status,)
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def delivery_status(self) -> dict:
        """Counts plus the most recent delivery outcome, for the GUI."""
        with self._lock:
            row = self._conn.execute(
                "SELECT incident_id, kind, status, sent_at, last_error, attempts FROM alerts "
                "WHERE status IN ('sent','failed','cancelled') ORDER BY COALESCE(sent_at, created_at) DESC, id DESC LIMIT 1"
            ).fetchone()
        last = None
        if row:
            last = {"id": row[0], "kind": row[1], "status": row[2], "sent_at": row[3], "error": row[4], "attempts": row[5]}
        return {"counts": self.counts(), "last": last}

    def requeue_failed(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE alerts SET status='pending', attempts=0, next_attempt_at=0 WHERE status='failed'"
            )
            return cur.rowcount

    def purge_sent(self, older_than_seconds: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM alerts WHERE status IN ('sent','cancelled') AND COALESCE(sent_at, created_at) < ?",
                (self.clock() - older_than_seconds,)
            )
            return cur.rowcount


class DeliveryWorker(threading.Thread):
    """Drains the queue using ``sender(payload, screenshot_path)``.

    ``sender`` raises :class:`DeliveryError` on failure.
    """

    def __init__(self, queue: DeliveryQueue, sender: Callable[[dict, str], None],
                 idle_sleep: float = 2.0, on_event: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(name="telegram-delivery", daemon=True)
        self.queue = queue
        self.sender = sender
        self.idle_sleep = idle_sleep
        self.on_event = on_event or (lambda msg: None)
        self._stop = threading.Event()
        self._wake = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        self._wake.set()

    def run(self) -> None:  # pragma: no cover - exercised through process_once in tests
        while not self._stop.is_set():
            if not self.process_once():
                wait = self.queue.seconds_until_next()
                timeout = self.idle_sleep if wait is None else min(self.idle_sleep, max(0.1, wait))
                self._wake.wait(timeout)
                self._wake.clear()

    def process_once(self) -> bool:
        """Deliver one due item. Returns True if an item was processed."""
        item = self.queue.next_due()
        if item is None:
            return False
        try:
            self.sender(item.payload, item.screenshot_path)
        except DeliveryError as exc:
            status = self.queue.mark_failed(item.id, str(exc), exc.retry_after, exc.permanent)
            self.on_event(
                f"alert {item.incident_id} delivery failed (attempt {item.attempts + 1}): {exc} -> {status}"
            )
            return True
        except Exception as exc:  # unexpected -> still retry, never crash the worker
            log.exception("unexpected delivery error")
            status = self.queue.mark_failed(item.id, f"{type(exc).__name__}: {exc}")
            self.on_event(f"alert {item.incident_id} delivery error: {exc} -> {status}")
            return True
        self.queue.mark_sent(item.id)
        self.on_event(f"alert {item.incident_id} delivered")
        return True
