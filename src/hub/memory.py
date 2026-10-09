"""Hub-side memory sync: resolved incidents and session/broadcast reports go
to Supermemory (key from the hub environment only) with workspace/device
scope; a ``memory_sync`` table makes the job idempotent. Retrieval is exposed
to the admin API and the /report command as labelled, untrusted reference."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from studio_monitor.memory import MemoryProvider, format_hits, incident_doc, make_provider, session_doc

from .db import Base, Device, EventRow, IncidentRow

log = logging.getLogger("hub.memory")
REPORT_TYPES = ("SESSION_REPORT", "BROADCAST_ENDED")


class MemorySyncRow(Base):
    __tablename__ = "memory_sync"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)     # incident:<id> | report:<event_id>
    synced_utc: Mapped[str] = mapped_column(String(40), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class HubMemory:
    def __init__(self, session_factory: sessionmaker[Session], api_key: str, namespace_prefix: str = "studio-hub",
                 client: Any = None, enabled: bool = True) -> None:
        self.sf = session_factory
        self._providers: dict[str, MemoryProvider] = {}
        self._key = api_key
        self._client = client
        self.prefix = namespace_prefix
        self.enabled = enabled and bool(api_key)
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return self.enabled

    def provider(self, workspace_id: str) -> Optional[MemoryProvider]:
        if not self.enabled:
            return None
        p = self._providers.get(workspace_id)
        if p is None:
            p = make_provider(True, f"{self.prefix}-{workspace_id[:8]}", self._key, client=self._client)
            self._providers[workspace_id] = p
        return p

    def sync_once(self, now_utc: str, limit: int = 50) -> dict:
        """Push unsynced resolved incidents and reports. Idempotent (stable doc ids + memory_sync rows)."""
        if not self.enabled:
            return {"incidents": 0, "reports": 0, "skipped": "no API key"}
        done = {"incidents": 0, "reports": 0, "failed": 0}
        with self.sf() as s:
            synced = {r.key for r in s.scalars(select(MemorySyncRow).where(MemorySyncRow.ok.is_(True)))}
            devices = {d.id: d for d in s.scalars(select(Device))}
            incs = [i for i in s.scalars(select(IncidentRow).where(IncidentRow.resolved_utc != "").order_by(IncidentRow.resolved_utc.desc()).limit(limit * 4))
                    if f"incident:{i.incident_id}" not in synced][:limit]
            for inc in incs:
                dev = devices.get(inc.device_id)
                p = self.provider(inc.workspace_id)
                if p is None:
                    continue
                doc = incident_doc(inc, dev.name if dev else inc.device_id[:8], dev.owner_label if dev else "", inc.workspace_id)
                ok = bool(p.add(doc))
                s.merge(MemorySyncRow(key=doc.id, synced_utc=now_utc, ok=ok, attempts=1))
                done["incidents" if ok else "failed"] += 1
            evs = [e for e in s.scalars(select(EventRow).where(EventRow.type.in_(REPORT_TYPES)).order_by(EventRow.received_utc.desc()).limit(limit * 4))
                   if f"report:{e.event_id}" not in synced and (e.detail or {}).get("report")][:limit]
            for e in evs:
                p = self.provider(e.workspace_id)
                if p is None:
                    continue
                report = dict((e.detail or {}).get("report") or {})
                report.setdefault("device_id", e.device_id)
                report["report_id"] = e.event_id
                doc = session_doc(report, e.workspace_id)
                ok = bool(p.add(doc))
                s.merge(MemorySyncRow(key=doc.id, synced_utc=now_utc, ok=ok, attempts=1))
                done["reports" if ok else "failed"] += 1
            s.commit()
        return done

    def search(self, workspace_id: str, query: str, device_id: str = "", limit: int = 5) -> list:
        p = self.provider(workspace_id)
        if p is None:
            return []
        scope = {"workspace_id": workspace_id}
        if device_id:
            scope["device_id"] = device_id
        return p.search(query, scope, limit)

    def similar_block(self, workspace_id: str, query: str, device_id: str = "") -> str:
        return format_hits(self.search(workspace_id, query, device_id), "Similar past incidents / sessions")
