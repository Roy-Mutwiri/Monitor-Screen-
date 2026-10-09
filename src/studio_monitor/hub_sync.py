"""Agent↔hub synchronisation: heartbeats every N seconds, outbox drain with
backoff, evidence upload, hub status for the UI. Runs on its own thread in
production (``start``) and inline via ``tick`` in tests.

Remote commands returned with a heartbeat are *predefined operations only*
(``commands`` list); this milestone records them and executes none.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .hub_client import HubClient, HubClientError
from .hub_outbox import HubOutbox

log = logging.getLogger(__name__)


@dataclass
class HubStatus:
    enabled: bool = False
    enrolled: bool = False
    connected: bool = False
    last_heartbeat_ok: float = 0.0
    last_error: str = ""
    pending: int = 0
    evidence_pending: int = 0
    rejected: int = 0
    uploaded_total: int = 0
    commands_seen: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "enrolled": self.enrolled, "connected": self.connected,
                "last_heartbeat_ok": self.last_heartbeat_ok, "last_error": self.last_error, "pending": self.pending,
                "evidence_pending": self.evidence_pending, "rejected": self.rejected, "uploaded_total": self.uploaded_total}


class HubSync:
    def __init__(self, client: HubClient, outbox: HubOutbox, status_provider: Callable[[], dict],
                 heartbeat_seconds: float = 15.0, upload_evidence: bool = True, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic, on_event: Optional[Callable[[str], None]] = None,
                 on_command: Optional[Callable[[dict], None]] = None, batch_size: int = 50) -> None:
        self.client = client
        self.outbox = outbox
        self.status_provider = status_provider
        self.heartbeat_seconds = heartbeat_seconds
        self.upload_evidence = upload_evidence
        self.clock, self.mono = clock, mono
        self.on_event = on_event or (lambda m: log.info(m))
        self.on_command = on_command
        self.batch_size = batch_size
        self.status = HubStatus(enabled=True, enrolled=bool(client.device_id and client.secret))
        self._next_heartbeat = 0.0
        self._hb_failures = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_purge = 0.0

    # ------------------------------------------------------------------
    def tick(self) -> None:
        if not self.status.enrolled:
            return
        now = self.mono()
        if now >= self._next_heartbeat:
            self._heartbeat()
        self._drain()
        self._upload_evidence()
        c = self.outbox.counts()
        self.status.pending, self.status.evidence_pending, self.status.rejected = c["pending"], c["evidence"], c["rejected"]
        if self.clock() - self._last_purge > 3600:
            self.outbox.purge()
            self._last_purge = self.clock()

    def _heartbeat(self) -> None:
        try:
            resp = self.client.heartbeat(self.status_provider())
        except HubClientError as exc:
            self._hb_failures += 1
            self.status.connected, self.status.last_error = False, str(exc)
            # failed heartbeats retry with backoff but never slower than the unreachable window
            self._next_heartbeat = self.mono() + min(60.0, self.heartbeat_seconds * (1 + self._hb_failures))
            if self._hb_failures in (1, 5, 20):
                self.on_event(f"hub heartbeat failed ({self._hb_failures}x): {exc}")
            return
        if not self.status.connected:
            self.on_event("hub connected")
        self._hb_failures = 0
        self.status.connected, self.status.last_error, self.status.last_heartbeat_ok = True, "", self.clock()
        self._next_heartbeat = self.mono() + self.heartbeat_seconds
        for cmd in resp.get("commands") or []:
            self.status.commands_seen.append(cmd)
            self.on_event(f"hub command received (recorded, not executed in this version): {str(cmd)[:120]}")
            if self.on_command:
                self.on_command(cmd)

    def _drain(self) -> None:
        items = self.outbox.due(self.batch_size)
        if not items:
            return
        try:
            res = self.client.post_events([it.event for it in items])
        except HubClientError as exc:
            if exc.permanent and exc.status == 401:
                self.status.connected, self.status.last_error = False, str(exc)
                self.outbox.mark_failed([it.event_id for it in items], str(exc))
                return
            delay = self.outbox.mark_failed([it.event_id for it in items], str(exc))
            self.status.last_error = str(exc)
            log.debug("hub upload failed, retry in %.0f s: %s", delay, exc)
            return
        by_id = {it.event_id: it for it in items}
        for eid in res.get("accepted", []) + res.get("duplicate", []):
            it = by_id.get(eid)
            needs = bool(it and it.evidence_path and self.upload_evidence and os.path.exists(it.evidence_path)
                         and eid in res.get("accepted", []))
            self.outbox.mark_accepted(eid, needs)
            self.status.uploaded_total += 1
        for eid, reason in (res.get("rejected") or {}).items():
            self.outbox.mark_rejected(eid, reason)
            self.on_event(f"hub rejected event {eid}: {reason}")

    def _upload_evidence(self) -> None:
        for it in self.outbox.evidence_due(3):
            try:
                self.client.upload_evidence(it.event_id, it.evidence_path, it.evidence_sha256)
                self.outbox.mark_done(it.event_id)
            except (OSError, HubClientError) as exc:
                if isinstance(exc, HubClientError) and exc.permanent or isinstance(exc, OSError):
                    self.outbox.mark_done(it.event_id)      # evidence is optional; never block the queue on it
                    log.info("evidence for %s not uploaded: %s", it.event_id, exc)
                else:
                    self.outbox.mark_failed([it.event_id], str(exc))

    # ------------------------------------------------------------------ thread
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # pragma: no cover - keep the loop alive
                log.exception("hub sync tick failed: %s", exc)
            self._stop.wait(1.0)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hub-sync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.client.close()
