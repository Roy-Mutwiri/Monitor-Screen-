"""Persistent (SQLite) outbox: one *event* (payload + immutable redacted
evidence) fans out to one *delivery* per enabled, subscribed bot. Each
delivery carries its own destination snapshot, attempts, backoff and result,
so a failing or rate-limited bot never blocks the others.

Alerts are written here *before* any network call, so a crash, a reboot or a
long Telegram outage never loses an alert. :class:`DeliveryWorker` drains the
queue with bounded concurrency (one in-flight delivery per bot at a time).

Delivery is at-least-once: after an ambiguous timeout Telegram may have
accepted the message and the retry can send it again. Exactly-once is not
claimed.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

log = logging.getLogger(__name__)

KIND_INCIDENT = "incident"
KIND_ACTIVITY = "activity"
KIND_REMINDER = "reminder"
KIND_STATUS = "status"
KIND_TEST = "test"

TERMINAL = ("sent", "failed", "cancelled", "dead")
SCHEMA_VERSION = 3


@dataclass
class Delivery:
    id: int
    event_id: str
    bot_id: str
    bot_name: str
    chat_id: str
    thread_id: Optional[int]
    status: str
    attempts: int
    last_error: str
    payload: dict
    evidence_path: str
    kind: str
    created_at: float
    message_id: Optional[int] = None
    completed_at: Optional[float] = None
    next_attempt_at: float = 0.0


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
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    evidence_path TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    bot_id TEXT NOT NULL,
    bot_name TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL,
    thread_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    message_id INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE(event_id, bot_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_due ON deliveries(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_bot ON deliveries(bot_id, status);
CREATE TABLE IF NOT EXISTS bot_state (
    bot_id TEXT PRIMARY KEY,
    blocked_until REAL NOT NULL DEFAULT 0,
    last_result TEXT NOT NULL DEFAULT '',
    last_result_at REAL
);
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
                 backoff_max: float = 300.0, clock: Callable[[], float] = time.time,
                 sanitizer: Callable[[str], str] = lambda s: s) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.clock = clock
        self.sanitize = sanitizer
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(alerts)")}
        if "kind" not in cols:
            self._conn.execute(f"ALTER TABLE alerts ADD COLUMN kind TEXT NOT NULL DEFAULT '{KIND_INCIDENT}'")
        if "migrated" not in cols:
            self._conn.execute("ALTER TABLE alerts ADD COLUMN migrated INTEGER NOT NULL DEFAULT 0")
        ecols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
        if "owner_label" not in ecols:
            self._conn.execute("ALTER TABLE events ADD COLUMN owner_label TEXT NOT NULL DEFAULT ''")
        if (self.get_state("schema_version") or 0) < SCHEMA_VERSION:
            self.set_state("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes atomically (e.g. event + deliveries + state)."""
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

    def set_state(self, key: str, value, conn: Optional[sqlite3.Connection] = None) -> None:
        sql = ("INSERT INTO kv_state(key, value, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at")
        if conn is not None:
            conn.execute(sql, (key, json.dumps(value), self.clock()))
            return
        with self._lock:
            self._conn.execute(sql, (key, json.dumps(value), self.clock()))

    # -- events + deliveries ----------------------------------------------
    def create_event(self, event_id: str, kind: str, category: str, payload: dict, evidence_path: str,
                     targets: list, label: str = "", conn: Optional[sqlite3.Connection] = None,
                     owner_label: str = "") -> int:
        """Persist one event and one delivery per target (atomic). Returns the
        number of deliveries created. Duplicate (event, bot) pairs are ignored.
        ``owner_label`` is the notification label in force when the event was
        created; later changes never rewrite it or the queued payload."""
        payload = dict(payload)
        payload.setdefault("created_at", self.clock())
        payload.setdefault("owner_label", owner_label)

        def _do(c: sqlite3.Connection) -> int:
            c.execute("INSERT OR IGNORE INTO events (event_id, kind, category, label, payload, evidence_path, "
                      "created_at, owner_label) VALUES (?,?,?,?,?,?,?,?)",
                      (event_id, kind, category, label[:200], json.dumps(payload), evidence_path, self.clock(),
                       owner_label[:80]))
            n = 0
            for t in targets:
                cur = c.execute(
                    "INSERT OR IGNORE INTO deliveries (event_id, bot_id, bot_name, chat_id, thread_id, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (event_id, t.bot_id, t.bot_name, t.chat_id, t.thread_id, self.clock()))
                n += cur.rowcount
            return n

        if conn is not None:
            return _do(conn)
        with self.transaction() as c:
            return _do(c)

    _DELIVERY_SQL = (
        "SELECT d.id, d.event_id, d.bot_id, d.bot_name, d.chat_id, d.thread_id, d.status, d.attempts, d.last_error, "
        "e.payload, e.evidence_path, e.kind, d.created_at, d.message_id, d.completed_at, d.next_attempt_at "
        "FROM deliveries d JOIN events e ON e.event_id = d.event_id ")

    @staticmethod
    def _row(r) -> Delivery:
        return Delivery(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], json.loads(r[9]), r[10], r[11],
                        r[12], r[13], r[14], r[15])

    def due_deliveries(self, exclude_bots: Optional[set] = None) -> list[Delivery]:
        """One due delivery per bot (oldest first), skipping bots that are
        rate-limited (``bot_state.blocked_until``) or currently in flight."""
        now = self.clock()
        with self._lock:
            rows = self._conn.execute(
                self._DELIVERY_SQL +
                "LEFT JOIN bot_state b ON b.bot_id = d.bot_id "
                "WHERE d.status='pending' AND d.next_attempt_at <= ? AND COALESCE(b.blocked_until, 0) <= ? "
                "ORDER BY d.id", (now, now)).fetchall()
        seen: set[str] = set(exclude_bots or ())
        out = []
        for r in rows:
            if r[2] in seen:
                continue
            seen.add(r[2])
            out.append(self._row(r))
        return out

    def seconds_until_next(self) -> Optional[float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(MAX(d.next_attempt_at, COALESCE(b.blocked_until, 0))) FROM deliveries d "
                "LEFT JOIN bot_state b ON b.bot_id = d.bot_id WHERE d.status='pending'").fetchone()
        if row is None or row[0] is None:
            return None
        return max(0.0, row[0] - self.clock())

    def mark_sent(self, delivery_id: int, message_id: Optional[int] = None) -> None:
        now = self.clock()
        with self._lock:
            row = self._conn.execute("SELECT bot_id FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            self._conn.execute(
                "UPDATE deliveries SET status='sent', completed_at=?, attempts=attempts+1, message_id=?, last_error='' "
                "WHERE id=?", (now, message_id, delivery_id))
            if row:
                self._bot_result(row[0], "sent", now, 0)

    def mark_failed(self, delivery_id: int, error: str, retry_after: Optional[float] = None,
                    permanent: bool = False) -> str:
        """Schedule a retry with exponential backoff, or give up. A
        ``retry_after`` hint blocks only this delivery's bot. Returns new status."""
        error = self.sanitize(error)[:500]
        now = self.clock()
        with self._lock:
            row = self._conn.execute("SELECT attempts, bot_id FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            bot_id = row[1] if row else ""
            if permanent or attempts >= self.max_attempts:
                status, next_at, completed = "failed", 0.0, now
            else:
                status, completed = "pending", None
                delay = min(self.backoff_max, self.backoff_base * (2 ** (attempts - 1)))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                next_at = now + delay
            self._conn.execute(
                "UPDATE deliveries SET status=?, attempts=?, next_attempt_at=?, last_error=?, completed_at=? WHERE id=?",
                (status, attempts, next_at, error, completed, delivery_id))
            if bot_id:
                self._bot_result(bot_id, f"{status}: {error}", now, now + retry_after if retry_after else 0)
            return status

    def _bot_result(self, bot_id: str, result: str, at: float, blocked_until: float) -> None:
        self._conn.execute(
            "INSERT INTO bot_state(bot_id, blocked_until, last_result, last_result_at) VALUES (?,?,?,?) "
            "ON CONFLICT(bot_id) DO UPDATE SET blocked_until=excluded.blocked_until, last_result=excluded.last_result, "
            "last_result_at=excluded.last_result_at", (bot_id, blocked_until, result[:300], at))

    def cancel_event(self, event_id: str, reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deliveries SET status='cancelled', last_error=?, completed_at=? WHERE event_id=? AND status='pending'",
                (reason[:500], self.clock(), event_id))
            return cur.rowcount

    def cancel_bot_pending(self, bot_id: str, reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deliveries SET status='cancelled', last_error=?, completed_at=? WHERE bot_id=? AND status='pending'",
                (reason[:500], self.clock(), bot_id))
            return cur.rowcount

    def retry_delivery(self, delivery_id: int) -> bool:
        """Re-queue one failed/dead delivery (never one that already succeeded)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deliveries SET status='pending', attempts=0, next_attempt_at=0, completed_at=NULL, "
                "last_error='' WHERE id=? AND status IN ('failed','dead','cancelled')", (delivery_id,))
            return cur.rowcount == 1

    def expire_stale(self, max_age_seconds: float) -> int:
        """Dead-letter deliveries still pending after ``max_age_seconds`` so a
        permanently blocked bot cannot keep evidence alive forever."""
        cutoff = self.clock() - max_age_seconds
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deliveries SET status='dead', completed_at=?, last_error=? WHERE status='pending' AND created_at < ?",
                (self.clock(), f"expired: not delivered within {int(max_age_seconds // 3600)} h", cutoff))
            return cur.rowcount

    def event_has_pending(self, event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM deliveries WHERE event_id=? AND status='pending'",
                                     (event_id,)).fetchone()
        return bool(row and row[0])

    def delivery(self, delivery_id: int) -> Optional[Delivery]:
        with self._lock:
            row = self._conn.execute(self._DELIVERY_SQL + "WHERE d.id=?", (delivery_id,)).fetchone()
        return self._row(row) if row else None

    def deliveries_for(self, event_id: str) -> list[Delivery]:
        with self._lock:
            rows = self._conn.execute(self._DELIVERY_SQL + "WHERE d.event_id=? ORDER BY d.id", (event_id,)).fetchall()
        return [self._row(r) for r in rows]

    def deliveries_for_bot(self, bot_id: str, limit: int = 50) -> list[Delivery]:
        with self._lock:
            rows = self._conn.execute(self._DELIVERY_SQL + "WHERE d.bot_id=? ORDER BY d.id DESC LIMIT ?",
                                      (bot_id, limit)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def summarize(deliveries: list[Delivery]) -> dict:
        c = {"total": len(deliveries), "sent": 0, "pending": 0, "failed": 0, "cancelled": 0, "dead": 0}
        for d in deliveries:
            c[d.status] = c.get(d.status, 0) + 1
        if c["total"] == 0:
            text = "no bots subscribed"
        else:
            parts = []
            if c["pending"]:
                parts.append(f"{c['pending']} retrying")
            blocked = c["failed"] + c["dead"]
            if blocked:
                parts.append(f"{blocked} blocked")
            if c["cancelled"]:
                parts.append(f"{c['cancelled']} cancelled")
            text = f"Delivered to {c['sent']} of {c['total']} bot{'s' if c['total'] != 1 else ''}"
            if parts:
                text += " — " + ", ".join(parts)
        c["text"] = text
        return c

    def event_summary(self, event_id: str) -> dict:
        return self.summarize(self.deliveries_for(event_id))

    def events_history(self, limit: int = 50, kind: str = "all") -> list[dict]:
        sql = "SELECT event_id, kind, category, label, evidence_path, created_at, owner_label FROM events"
        params: list = []
        if kind == "incident":
            sql += " WHERE kind=?"; params.append(KIND_INCIDENT)
        elif kind == "activity":
            sql += " WHERE kind IN (?,?,?,?)"; params += [KIND_ACTIVITY, KIND_REMINDER, KIND_STATUS, KIND_TEST]
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            out.append({"event_id": r[0], "kind": r[1], "category": r[2], "label": r[3], "evidence_path": r[4],
                        "created_at": r[5], "owner_label": r[6], "summary": self.event_summary(r[0])})
        return out

    def evidence_in_use(self) -> set[str]:
        """Evidence files still needed by a pending delivery."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT e.evidence_path FROM events e JOIN deliveries d ON d.event_id=e.event_id "
                "WHERE d.status='pending' AND e.evidence_path != ''").fetchall()
        return {r[0] for r in rows}

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status").fetchall()
        out = {"pending": 0, "sent": 0, "failed": 0, "cancelled": 0, "dead": 0}
        out.update({r[0]: r[1] for r in rows})
        return out

    def counts_by_kind(self, status: str = "pending") -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.kind, COUNT(*) FROM deliveries d JOIN events e ON e.event_id=d.event_id WHERE d.status=? "
                "GROUP BY e.kind", (status,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def events_by_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events_history(1000, "all") if e["kind"] == kind]

    def bot_stats(self, bot_id: str) -> dict:
        with self._lock:
            pend = self._conn.execute("SELECT COUNT(*) FROM deliveries WHERE bot_id=? AND status='pending'",
                                      (bot_id,)).fetchone()[0]
            st = self._conn.execute("SELECT last_result, last_result_at, blocked_until FROM bot_state WHERE bot_id=?",
                                    (bot_id,)).fetchone()
        return {"pending": pend, "last_result": st[0] if st else "", "last_result_at": st[1] if st else None,
                "blocked_until": st[2] if st else 0}

    def delivery_status(self) -> dict:
        """Counts plus the most recent delivery outcome, for the GUI."""
        with self._lock:
            row = self._conn.execute(
                "SELECT d.event_id, e.kind, d.status, d.completed_at, d.last_error, d.attempts, d.bot_name "
                "FROM deliveries d JOIN events e ON e.event_id=d.event_id WHERE d.status IN ('sent','failed','cancelled','dead') "
                "ORDER BY d.completed_at DESC, d.id DESC LIMIT 1").fetchone()
        last = None
        if row:
            last = {"id": row[0], "kind": row[1], "status": row[2], "sent_at": row[3], "error": row[4],
                    "attempts": row[5], "bot": row[6]}
        return {"counts": self.counts(), "last": last}

    def requeue_failed(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE deliveries SET status='pending', attempts=0, next_attempt_at=0, completed_at=NULL "
                "WHERE status IN ('failed','dead')")
            return cur.rowcount

    def purge_sent(self, older_than_seconds: float) -> int:
        cutoff = self.clock() - older_than_seconds
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM deliveries WHERE status IN ('sent','cancelled','failed','dead') AND created_at < ?", (cutoff,))
            self._conn.execute(
                "DELETE FROM events WHERE created_at < ? AND event_id NOT IN (SELECT event_id FROM deliveries)", (cutoff,))
            return cur.rowcount

    # -- legacy single-bot migration --------------------------------------
    def migrate_legacy_alerts(self, target) -> dict:
        """Turn rows of the old ``alerts`` table into events + deliveries for the
        migrated Default Bot. Pending alerts become pending deliveries (once);
        sent/failed/cancelled alerts become history rows and are never resent."""
        done = {"pending": 0, "history": 0}
        with self.transaction() as c:
            rows = c.execute("SELECT id, incident_id, payload, screenshot_path, status, created_at, sent_at, "
                             "last_error, kind FROM alerts WHERE migrated=0 ORDER BY id").fetchall()
            for (aid, incident_id, payload, shot, status, created, sent_at, err, kind) in rows:
                event_id = f"{incident_id}-L{aid}" if incident_id in ("STATUS",) else incident_id
                c.execute("INSERT OR IGNORE INTO events (event_id, kind, category, label, payload, evidence_path, "
                          "created_at) VALUES (?,?,?,?,?,?,?)",
                          (event_id, kind or KIND_INCIDENT, "legacy", incident_id, payload, shot, created))
                if target is None:
                    continue
                dstatus = "pending" if status == "pending" else status
                c.execute(
                    "INSERT OR IGNORE INTO deliveries (event_id, bot_id, bot_name, chat_id, thread_id, status, "
                    "attempts, created_at, completed_at, last_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (event_id, target.bot_id, target.bot_name, target.chat_id, target.thread_id, dstatus,
                     0, created, sent_at, err or ""))
                c.execute("UPDATE alerts SET migrated=1 WHERE id=?", (aid,))
                done["pending" if dstatus == "pending" else "history"] += 1
        return done

    def legacy_pending_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM alerts WHERE migrated=0 AND status='pending'").fetchone()[0]

    # -- local history tables (unchanged API) ------------------------------
    def record_incident(self, incident_id: str, category: str, label: str, text: str,
                        window_title: str, is_dialog: bool, screenshot_path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                (incident_id, category, label, text, window_title, int(is_dialog), screenshot_path, self.clock()))

    def recent_incidents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT incident_id, category, label, detected_text, window_title, is_dialog, "
                "screenshot_path, created_at FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ["incident_id", "category", "label", "detected_text", "window_title", "is_dialog",
                "screenshot_path", "created_at"]
        return [dict(zip(keys, r)) for r in rows]

    def record_event(self, event_id: str, event_type: str, ts_utc: str, details: Optional[dict] = None,
                     session_id: str = "", episode_id: str = "", screenshot_path: str = "",
                     alert_id: Optional[int] = None, conn: Optional[sqlite3.Connection] = None) -> None:
        sql = "INSERT OR REPLACE INTO activity_events VALUES (?,?,?,?,?,?,?,?,?)"
        args = (event_id, event_type, session_id, episode_id, ts_utc, json.dumps(details or {}),
                screenshot_path, alert_id, self.clock())
        if conn is not None:
            conn.execute(sql, args)
            return
        with self._lock:
            self._conn.execute(sql, args)

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
        """Unified history for the GUI/CLI: events with delivery summaries."""
        items = []
        for e in self.events_history(limit, kind):
            items.append({"ts": e["created_at"], "kind": e["kind"], "id": e["event_id"], "label": e["label"],
                          "detail": e["summary"]["text"], "summary": e["summary"], "evidence_path": e["evidence_path"],
                          "owner_label": e.get("owner_label", "")})
        return items


class DeliveryWorker(threading.Thread):
    """Drains the queue: one in-flight delivery per bot, up to ``concurrency``
    bots at once. ``send(delivery) -> message_id`` raises DeliveryError."""

    def __init__(self, queue: DeliveryQueue, send: Callable[[Delivery], Optional[int]],
                 concurrency: int = 4, idle_sleep: float = 2.0,
                 on_event: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(name="telegram-delivery", daemon=True)
        self.queue = queue
        self.send = send
        self.concurrency = max(1, concurrency)
        self.idle_sleep = idle_sleep
        self.on_event = on_event or (lambda msg: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        self._wake.set()

    def run(self) -> None:  # pragma: no cover - exercised through process_round in tests
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="tg-send")
        try:
            while not self._stop.is_set():
                if not self.process_round(parallel=True):
                    wait = self.queue.seconds_until_next()
                    timeout = self.idle_sleep if wait is None else min(self.idle_sleep, max(0.1, wait))
                    self._wake.wait(timeout)
                    self._wake.clear()
        finally:
            self._pool.shutdown(wait=True)

    def process_round(self, parallel: bool = False) -> int:
        """Deliver one due item per bot. Returns the number of deliveries attempted."""
        with self._inflight_lock:
            busy = set(self._inflight)
        due = self.queue.due_deliveries(exclude_bots=busy)[: self.concurrency]
        if not due:
            return 0
        if parallel and self._pool is not None:
            for d in due:
                with self._inflight_lock:
                    self._inflight.add(d.bot_id)
                self._pool.submit(self._deliver, d)
        else:
            for d in due:
                self._deliver(d)
        return len(due)

    def _deliver(self, d: Delivery) -> None:
        try:
            try:
                message_id = self.send(d)
            except DeliveryError as exc:
                status = self.queue.mark_failed(d.id, str(exc), exc.retry_after, exc.permanent)
                self.on_event(f"{d.event_id} -> {d.bot_name}: delivery failed (attempt {d.attempts + 1}): "
                              f"{self.queue.sanitize(str(exc))} -> {status}")
                return
            except Exception as exc:  # unexpected -> retry, never crash the worker
                log.exception("unexpected delivery error")
                status = self.queue.mark_failed(d.id, f"{type(exc).__name__}: {exc}")
                self.on_event(f"{d.event_id} -> {d.bot_name}: delivery error -> {status}")
                return
            self.queue.mark_sent(d.id, message_id)
            self.on_event(f"{d.event_id} -> {d.bot_name}: delivered")
        finally:
            with self._inflight_lock:
                self._inflight.discard(d.bot_id)
            self._wake.set()
