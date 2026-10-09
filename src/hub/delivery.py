"""Hub-side Telegram delivery and the reachability sweeper.

Reuses the agent's stdlib Telegram client (token redaction, retries, photo
captions). Bot tokens come from the hub process environment via each route's
``token_env`` name and never leave the hub."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

from studio_monitor.config import TelegramConfig
from studio_monitor.queue import DeliveryError
from studio_monitor.telegram import TelegramClient, deliver, sanitize

from .services import HubService

log = logging.getLogger("hub.delivery")


class HubDeliveryWorker:
    def __init__(self, session_factory: sessionmaker[Session], api_base: str = "https://api.telegram.org",
                 env: Optional[dict] = None, transport=None, clock: Callable[[], float] = time.time,
                 evidence_dir: str = "") -> None:
        self.sf = session_factory
        self.cfg = TelegramConfig(api_base=api_base)
        self.env = env if env is not None else os.environ
        self.transport = transport
        self.clock = clock
        self.evidence_dir = evidence_dir

    def process_round(self, limit: int = 20) -> int:
        done = 0
        with self.sf() as s:
            svc = HubService(s, self.clock)
            for d, ev, route in svc.due_deliveries(limit):
                token = self.env.get(route.token_env, "")
                if not token:
                    svc.mark_delivery(d, False, f"bot token env {route.token_env} is not set on the hub", permanent=False)
                    continue
                client = TelegramClient(self.cfg, token, route.chat_id, int(route.thread_id) if route.thread_id else None,
                                        transport=self.transport)
                payload = dict(ev.payload or {})
                payload.setdefault("text", ev.summary)
                payload.setdefault("caption", payload["text"])
                payload.setdefault("created_at", self.clock())
                path = ev.evidence_stored_path if ev.evidence_stored_path and os.path.exists(ev.evidence_stored_path) else ""
                try:
                    result = deliver(client, payload, path, self.clock)
                    mid = result.get("message_id") if isinstance(result, dict) else 0
                    svc.mark_delivery(d, True, message_id=int(mid or 0))
                except DeliveryError as exc:
                    svc.mark_delivery(d, False, sanitize(str(exc)), permanent=exc.permanent)
                except Exception as exc:  # pragma: no cover - defensive
                    svc.mark_delivery(d, False, sanitize(str(exc)))
                done += 1
            s.commit()
        return done


class HubBackground:
    """Sweeper + delivery loops (started from the FastAPI lifespan)."""

    def __init__(self, session_factory: sessionmaker[Session], worker: HubDeliveryWorker, unreachable_after: int,
                 interval: float = 5.0, retention_days: int = 90, clock: Callable[[], float] = time.time) -> None:
        self.sf = session_factory
        self.clock = clock
        self.worker = worker
        self.unreachable_after = unreachable_after
        self.interval = interval
        self.retention_days = retention_days
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_purge = 0.0
        self.commands = None          # HubCommandPollers, attached by create_app

    def once(self) -> dict:
        with self.sf() as s:
            svc = HubService(s, self.clock, unreachable_after=self.unreachable_after)
            flipped = svc.sweep_unreachable()
            purged = 0
            if self.clock() - self._last_purge > 3600:
                purged = svc.purge(self.retention_days)
                self._last_purge = self.clock()
            s.commit()
        delivered = self.worker.process_round()
        commands = 0
        if self.commands is not None:
            try:
                self.commands.env = self.worker.env
                commands = self.commands.poll_once()
            except Exception as exc:  # pragma: no cover
                log.error("command polling failed: %s", sanitize(str(exc)))
        return {"unreachable": len(flipped), "delivered": delivered, "purged": purged, "commands": commands}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.once()
            except Exception as exc:  # pragma: no cover
                log.error("background round failed: %s", sanitize(str(exc)))
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hub-background", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
