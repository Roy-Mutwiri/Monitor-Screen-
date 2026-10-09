"""Hub domain logic (framework-free, exercised directly by tests).

* enrollment with single-use, expiring pairing codes and per-device secrets
* authenticated ingestion with ``event_id`` dedup and device-scope enforcement
* heartbeats and the unreachable sweep ("Device unreachable — heartbeat missing")
* incident mirror (open / occurrence / resolve / ack / snooze)
* Telegram routing decisions for managed devices and hub-originated events
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from studio_monitor.contracts.events import EVENT_TYPES, SEVERITIES, Severity, utc_now_iso, validate_event

from .db import AuditRow, DeliveryRow, Device, EventRow, HeartbeatRow, IncidentRow, PairingCode, Route, Workspace

RESOLVING_TYPES = {"INCIDENT_RESOLVED", "HEALTH_RECOVERED", "DEVICE_REACHABLE", "BROADCAST_RECONNECTED"}
NON_INCIDENT_TYPES = {"STUDIO_OPENED", "STUDIO_ALREADY_RUNNING", "STUDIO_CLOSED", "BROADCAST_STARTED", "BROADCAST_ALREADY_LIVE",
                      "BROADCAST_ENDED", "NOT_LIVE_REMINDER", "MAINTENANCE_ENTER", "MAINTENANCE_EXIT", "MONITORING_GAP", "TEST",
                      "INCIDENT_ESCALATION"} | RESOLVING_TYPES
SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.URGENT: 2}


def now_iso(clock: Callable[[], float] | None = None) -> str:
    if clock is None:
        return utc_now_iso()
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat(timespec="seconds")


def parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _hash(value: str, salt: str = "") -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def format_pairing_code(raw: str) -> str:
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


class HubError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class IngestResult:
    accepted: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    routed: int = 0

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "duplicate": self.duplicate, "rejected": self.rejected, "routed": self.routed}


class HubService:
    def __init__(self, session: Session, clock: Callable[[], float] | None = None, pairing_ttl: int = 900,
                 unreachable_after: int = 90) -> None:
        self.s = session
        self.clock = clock
        self.pairing_ttl = pairing_ttl
        self.unreachable_after = unreachable_after

    # ------------------------------------------------------------- helpers
    def now(self) -> str:
        return now_iso(self.clock)

    def now_dt(self) -> datetime:
        return parse_iso(self.now())

    def audit(self, actor: str, action: str, target: str = "", **detail) -> None:
        self.s.add(AuditRow(ts_utc=self.now(), actor=actor, action=action, target=target, detail=detail))

    # ------------------------------------------------------------- workspaces
    def ensure_workspace(self, name: str) -> Workspace:
        ws = self.s.scalar(select(Workspace).where(Workspace.name == name))
        if ws is None:
            ws = Workspace(id=str(uuid.uuid4()), name=name, created_utc=self.now())
            self.s.add(ws)
            self.s.flush()
        return ws

    def workspaces(self) -> list[Workspace]:
        return list(self.s.scalars(select(Workspace).order_by(Workspace.name)))

    # ------------------------------------------------------------- pairing / enrollment
    def create_pairing_code(self, workspace_id: str, label: str = "", created_by: str = "admin",
                            ttl_seconds: Optional[int] = None) -> tuple[str, PairingCode]:
        """Returns the plaintext code exactly once; only its hash is stored."""
        raw = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12))
        code = format_pairing_code(raw)
        ttl = self.pairing_ttl if ttl_seconds is None else ttl_seconds
        expires = (self.now_dt() + timedelta(seconds=ttl)).isoformat(timespec="seconds")
        row = PairingCode(code_hash=_hash(raw), workspace_id=workspace_id, label=label, created_utc=self.now(),
                          expires_utc=expires, created_by=created_by)
        self.s.add(row)
        self.audit(created_by, "pairing_code.create", workspace_id, label=label, expires_utc=expires)
        self.s.flush()
        return code, row

    def enroll(self, code: str, device_id: str, name: str = "", hostname: str = "", agent_version: str = "",
               mode: str = "standalone", owner_label: str = "", expected_account: str = "") -> tuple[Device, str]:
        raw = code.replace("-", "").strip().upper()
        if not raw:
            raise HubError("pairing code required", 400)
        pc = self.s.scalar(select(PairingCode).where(PairingCode.code_hash == _hash(raw)))
        if pc is None:
            raise HubError("unknown pairing code", 403)
        if pc.used_utc:
            raise HubError("pairing code already used", 403)
        if (parse_iso(pc.expires_utc) or self.now_dt()) <= self.now_dt():
            raise HubError("pairing code expired", 403)
        try:
            uuid.UUID(device_id)
        except (ValueError, TypeError):
            raise HubError("device_id must be a UUID", 400)
        if mode not in ("standalone", "managed"):
            raise HubError("mode must be standalone or managed", 400)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        dev = self.s.get(Device, device_id)
        if dev is None:
            dev = Device(id=device_id, workspace_id=pc.workspace_id)
            self.s.add(dev)
        elif dev.workspace_id != pc.workspace_id:
            raise HubError("device belongs to another workspace", 403)
        dev.name, dev.hostname, dev.agent_version = name[:120], hostname[:120], agent_version[:40]
        dev.mode, dev.owner_label, dev.expected_account = mode, owner_label[:120], expected_account[:120]
        dev.enrolled_utc, dev.token_salt, dev.token_hash, dev.revoked = self.now(), salt, _hash(secret, salt), False
        pc.used_utc, pc.used_by_device = self.now(), device_id
        self.audit("agent", "device.enroll", device_id, name=name, mode=mode, workspace=pc.workspace_id)
        self.s.flush()
        return dev, secret

    def authenticate(self, device_id: str, secret: str) -> Optional[Device]:
        dev = self.s.get(Device, device_id)
        if dev is None or dev.revoked or not dev.token_hash:
            return None
        if not secrets.compare_digest(_hash(secret, dev.token_salt), dev.token_hash):
            return None
        return dev

    def revoke(self, device_id: str, actor: str = "admin") -> None:
        dev = self.s.get(Device, device_id)
        if dev is None:
            raise HubError("unknown device", 404)
        dev.revoked = True
        self.audit(actor, "device.revoke", device_id)

    def update_device(self, device_id: str, actor: str = "admin", **fields) -> Device:
        dev = self.s.get(Device, device_id)
        if dev is None:
            raise HubError("unknown device", 404)
        for k in ("name", "owner_label", "expected_account", "mode"):
            if k in fields and fields[k] is not None:
                setattr(dev, k, str(fields[k])[:120])
        self.audit(actor, "device.update", device_id, **{k: v for k, v in fields.items() if v is not None})
        return dev

    # ------------------------------------------------------------- ingestion
    def ingest(self, device: Device, events: list[dict]) -> IngestResult:
        res = IngestResult()
        if len(events) > 200:
            raise HubError("at most 200 events per request", 413)
        ids = [e.get("event_id") for e in events if isinstance(e, dict) and isinstance(e.get("event_id"), str)]
        existing = set(self.s.scalars(select(EventRow.event_id).where(EventRow.event_id.in_(ids)))) if ids else set()
        seen: set[str] = set()
        for e in events:
            errs = validate_event(e)
            eid = e.get("event_id") if isinstance(e, dict) else None
            if errs:
                res.rejected[str(eid or "?")] = "; ".join(errs)
                continue
            if e["device_id"] != device.id:
                res.rejected[eid] = "device_id does not match the authenticated device"   # agents write only their own telemetry
                continue
            if eid in existing or eid in seen:
                res.duplicate.append(eid)
                continue
            seen.add(eid)
            row = self._store_event(device, e)
            res.accepted.append(eid)
            self._mirror_incident(device, row)
            if e.get("account"):
                device.observed_account = str(e["account"])[:120]
            if device.mode == "managed":
                res.routed += self.route_event(row, device)
        self.s.flush()
        return res

    def _store_event(self, device: Device, e: dict, synthetic: bool = False) -> EventRow:
        ev = e.get("evidence") or {}
        row = EventRow(
            event_id=e["event_id"], device_id=device.id, workspace_id=device.workspace_id, type=e["type"],
            severity=e.get("severity") or EVENT_TYPES.get(e["type"], Severity.INFO), category=e.get("category", "") or "",
            session_id=e.get("session_id", "") or "", incident_id=e.get("incident_id", "") or "",
            observed_utc=e["observed_utc"], received_utc=self.now(), summary=(e.get("summary") or "")[:1000],
            account=e.get("account", "") or "", owner_label=e.get("owner_label", "") or "", validity=e.get("validity", "valid"),
            detail=e.get("detail") or {}, payload=e.get("payload") or {}, evidence_sha256=ev.get("sha256", "") or "",
            evidence_size=int(ev.get("size", 0) or 0), synthetic=synthetic,
        )
        self.s.add(row)
        return row

    def _mirror_incident(self, device: Device, row: EventRow) -> None:
        if not row.incident_id:
            return
        inc = self.s.get(IncidentRow, row.incident_id)
        if row.type in RESOLVING_TYPES:
            if inc is not None and not inc.resolved_utc:
                inc.resolved_utc, inc.resolution, inc.last_event_id = row.observed_utc, row.summary, row.event_id
            return
        if row.type in NON_INCIDENT_TYPES:
            if inc is not None:
                inc.occurrences += 1
                inc.last_event_id = row.event_id
            return
        if inc is None:
            self.s.add(IncidentRow(incident_id=row.incident_id, device_id=device.id, workspace_id=device.workspace_id,
                                   category=row.category, type=row.type, severity=row.severity, summary=row.summary,
                                   opened_utc=row.observed_utc, last_event_id=row.event_id, account=row.account))
        else:
            inc.occurrences += 1
            inc.last_event_id = row.event_id
            if SEVERITY_RANK.get(row.severity, 0) > SEVERITY_RANK.get(inc.severity, 0):
                inc.severity = row.severity
            if inc.resolved_utc:   # re-opened occurrence of a resolved incident id (agent restart) -> keep history honest
                inc.resolved_utc, inc.resolution = "", ""

    # ------------------------------------------------------------- heartbeats / reachability
    def heartbeat(self, device: Device, status: dict) -> dict:
        now = self.now()
        was_unreachable = device.reachable is False
        device.last_heartbeat_utc, device.last_status, device.reachable = now, dict(status or {}), True
        if "mode" in status and status["mode"] in ("standalone", "managed"):
            device.mode = status["mode"]
        if status.get("account"):
            device.observed_account = str(status["account"])[:120]
        self.s.add(HeartbeatRow(device_id=device.id, received_utc=now, status=dict(status or {})))
        if was_unreachable:
            device.unreachable_since_utc = ""
            self._synthetic_event(device, "DEVICE_REACHABLE", f"Device {device.name or device.id} is reachable again (heartbeat received).",
                                  incident_id=f"UNR-{device.id}", resolve=True)
        commands = list(device.pending_commands or [])
        device.pending_commands = []
        self.s.flush()
        return {"ok": True, "server_utc": now, "commands": commands}

    def sweep_unreachable(self) -> list[Device]:
        """Mark devices whose heartbeat is older than ``unreachable_after`` and
        raise one DEVICE_UNREACHABLE incident each (resolved on the next heartbeat)."""
        cutoff = self.now_dt() - timedelta(seconds=self.unreachable_after)
        flipped = []
        for dev in self.s.scalars(select(Device).where(Device.revoked.is_(False), Device.reachable.is_(True))):
            hb = parse_iso(dev.last_heartbeat_utc)
            if hb is None or hb <= cutoff:
                dev.reachable = False
                dev.unreachable_since_utc = self.now()
                missing = int((self.now_dt() - hb).total_seconds()) if hb else self.unreachable_after
                self._synthetic_event(dev, "DEVICE_UNREACHABLE",
                                      f"Device unreachable — heartbeat missing for {missing} s ({dev.name or dev.id}).",
                                      incident_id=f"UNR-{dev.id}")
                flipped.append(dev)
        self.s.flush()
        return flipped

    def _synthetic_event(self, device: Device, type_: str, summary: str, incident_id: str = "", resolve: bool = False) -> EventRow:
        e = {"event_id": str(uuid.uuid4()), "schema_version": 1, "device_id": device.id, "type": type_, "summary": summary,
             "severity": EVENT_TYPES.get(type_, Severity.INFO), "category": "health", "observed_utc": self.now(),
             "incident_id": incident_id, "owner_label": device.owner_label, "account": device.observed_account,
             "payload": {"text": summary, "caption": summary}}
        row = self._store_event(device, e, synthetic=True)
        self._mirror_incident(device, row)
        # hub-originated events are always routed (the agent cannot report its own absence)
        self.route_event(row, device)
        return row

    # ------------------------------------------------------------- incidents
    def incident(self, incident_id: str) -> IncidentRow:
        inc = self.s.get(IncidentRow, incident_id)
        if inc is None:
            raise HubError("unknown incident", 404)
        return inc

    def ack(self, incident_id: str, actor: str) -> IncidentRow:
        inc = self.incident(incident_id)
        inc.acked_utc, inc.acked_by = self.now(), actor[:120]
        self.audit(actor, "incident.ack", incident_id)
        return inc

    def snooze(self, incident_id: str, seconds: int, actor: str) -> IncidentRow:
        inc = self.incident(incident_id)
        seconds = max(60, min(int(seconds), 24 * 3600))
        inc.snoozed_until_utc = (self.now_dt() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        self.audit(actor, "incident.snooze", incident_id, seconds=seconds)
        return inc

    def resolve(self, incident_id: str, text: str, actor: str) -> IncidentRow:
        inc = self.incident(incident_id)
        if not inc.resolved_utc:
            inc.resolved_utc, inc.resolution = self.now(), text[:1000]
        self.audit(actor, "incident.resolve", incident_id, text=text[:200])
        return inc

    def open_incidents(self, workspace_id: Optional[str] = None) -> list[IncidentRow]:
        q = select(IncidentRow).where(IncidentRow.resolved_utc == "").order_by(IncidentRow.opened_utc.desc())
        if workspace_id:
            q = q.where(IncidentRow.workspace_id == workspace_id)
        return list(self.s.scalars(q))

    # ------------------------------------------------------------- routing
    def add_route(self, workspace_id: str, name: str, token_env: str, chat_id: str, thread_id: str = "",
                  categories: Optional[list[str]] = None, min_severity: str = Severity.INFO, actor: str = "admin") -> Route:
        if min_severity not in SEVERITIES:
            raise HubError("bad severity", 400)
        r = Route(workspace_id=workspace_id, name=name[:120], token_env=token_env[:120], chat_id=str(chat_id), thread_id=str(thread_id or ""),
                  categories=list(categories or []), min_severity=min_severity)
        self.s.add(r)
        self.audit(actor, "route.add", name, token_env=token_env, chat_id=str(chat_id))
        self.s.flush()
        return r

    def route_event(self, row: EventRow, device: Device) -> int:
        inc = self.s.get(IncidentRow, row.incident_id) if row.incident_id else None
        if inc is not None and inc.snoozed_until_utc and row.type not in RESOLVING_TYPES:
            until = parse_iso(inc.snoozed_until_utc)
            if until and until > self.now_dt():
                return 0
        n = 0
        for r in self.s.scalars(select(Route).where(Route.workspace_id == device.workspace_id, Route.enabled.is_(True))):
            if r.categories and row.category not in r.categories:
                continue
            if SEVERITY_RANK.get(row.severity, 0) < SEVERITY_RANK.get(r.min_severity, 0) and row.type not in RESOLVING_TYPES:
                continue
            self.s.add(DeliveryRow(event_id=row.event_id, route_id=r.id, next_attempt_utc=self.now()))
            n += 1
        return n

    def due_deliveries(self, limit: int = 20) -> list[tuple[DeliveryRow, EventRow, Route]]:
        now = self.now()
        q = (select(DeliveryRow, EventRow, Route).join(EventRow, EventRow.event_id == DeliveryRow.event_id)
             .join(Route, Route.id == DeliveryRow.route_id)
             .where(DeliveryRow.status.in_(["pending", "failed"]), DeliveryRow.next_attempt_utc <= now)
             .order_by(DeliveryRow.id).limit(limit))
        return [tuple(r) for r in self.s.execute(q)]

    def mark_delivery(self, d: DeliveryRow, ok: bool, error: str = "", message_id: int = 0, permanent: bool = False,
                      max_attempts: int = 12) -> None:
        d.attempts += 1
        if ok:
            d.status, d.message_id, d.sent_utc, d.last_error = "sent", int(message_id or 0), self.now(), ""
            return
        d.last_error = error[:500]
        if permanent or d.attempts >= max_attempts:
            d.status = "dead"
            return
        backoff = min(3600, 5 * (2 ** (d.attempts - 1)))
        d.status = "failed"
        d.next_attempt_utc = (self.now_dt() + timedelta(seconds=backoff)).isoformat(timespec="seconds")

    # ------------------------------------------------------------- dashboard queries
    def devices(self, workspace_id: Optional[str] = None) -> list[Device]:
        q = select(Device).order_by(Device.name, Device.id)
        if workspace_id:
            q = q.where(Device.workspace_id == workspace_id)
        return list(self.s.scalars(q))

    def device_status(self, dev: Device) -> str:
        if dev.revoked:
            return "REVOKED"
        if dev.reachable is None:
            return "NEVER_SEEN"
        if dev.reachable is False:
            return "UNREACHABLE"
        st = dev.last_status or {}
        if st.get("live_state") == "LIVE":
            return "LIVE"
        if st.get("app_state") == "RUNNING":
            return "STUDIO_OPEN"
        return "ONLINE"

    def recent_events(self, device_id: Optional[str] = None, limit: int = 100, types: Optional[list[str]] = None) -> list[EventRow]:
        q = select(EventRow).order_by(EventRow.received_utc.desc(), EventRow.event_id).limit(limit)
        if device_id:
            q = q.where(EventRow.device_id == device_id)
        if types:
            q = q.where(EventRow.type.in_(types))
        return list(self.s.scalars(q))

    def counts(self) -> dict:
        return {
            "devices": self.s.scalar(select(func.count()).select_from(Device)) or 0,
            "open_incidents": self.s.scalar(select(func.count()).select_from(IncidentRow).where(IncidentRow.resolved_utc == "")) or 0,
            "events": self.s.scalar(select(func.count()).select_from(EventRow)) or 0,
            "pending_deliveries": self.s.scalar(select(func.count()).select_from(DeliveryRow).where(DeliveryRow.status.in_(["pending", "failed"]))) or 0,
        }

    def purge(self, retention_days: int) -> int:
        cutoff = (self.now_dt() - timedelta(days=retention_days)).isoformat(timespec="seconds")
        n = 0
        for row in self.s.scalars(select(HeartbeatRow).where(HeartbeatRow.received_utc < cutoff)):
            self.s.delete(row); n += 1
        return n
