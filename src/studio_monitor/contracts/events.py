"""Event contract (schema version 1).

Every event carries a stable UUID, the device UUID, optional session /
incident ids, type, severity, the observed UTC time (agent clock) and the
received UTC time (server clock, set by the hub), an owner/account snapshot,
detector evidence validity and an evidence attachment reference. Uploads are
at-least-once; the hub deduplicates by ``event_id``.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = 1


class Severity:
    INFO = "INFO"
    WARNING = "WARNING"
    URGENT = "URGENT"


SEVERITIES = (Severity.INFO, Severity.WARNING, Severity.URGENT)

EVENT_TYPES: dict[str, str] = {
    # type -> default severity
    "RESTRICTION": Severity.URGENT,
    "CONTENT_WARNING": Severity.WARNING,
    "ACCOUNT_SUSPENSION": Severity.URGENT,
    "LIVE_INTERRUPTED": Severity.URGENT,
    "VERIFICATION": Severity.URGENT,
    "STUDIO_OPENED": Severity.INFO,
    "STUDIO_ALREADY_RUNNING": Severity.INFO,
    "STUDIO_CLOSED": Severity.INFO,
    "STUDIO_EXITED_UNEXPECTEDLY": Severity.WARNING,
    "BROADCAST_STARTED": Severity.INFO,
    "BROADCAST_ALREADY_LIVE": Severity.INFO,
    "BROADCAST_ENDED": Severity.INFO,
    "BROADCAST_END_REQUESTED": Severity.INFO,
    "BROADCAST_RECONNECTING": Severity.WARNING,
    "BROADCAST_RECONNECTED": Severity.INFO,
    "NOT_LIVE_REMINDER": Severity.INFO,
    "SCHEDULE_MISSED_START": Severity.WARNING,
    "HEALTH_DEGRADED": Severity.WARNING,
    "HEALTH_RECOVERED": Severity.INFO,
    "FACE_ABSENT": Severity.WARNING,
    "FACE_MOTION_LOW": Severity.WARNING,
    "PREVIEW_FROZEN": Severity.WARNING,
    "SOURCE_MISSING": Severity.WARNING,
    "BLACK_PREVIEW": Severity.WARNING,
    "AUDIO_SILENCE": Severity.WARNING,
    "PC_HEALTH": Severity.WARNING,
    "ACCOUNT_MISMATCH": Severity.WARNING,
    "MAINTENANCE_ENTER": Severity.INFO,
    "MAINTENANCE_EXIT": Severity.INFO,
    "MONITORING_GAP": Severity.INFO,
    "DEVICE_UNREACHABLE": Severity.WARNING,
    "DEVICE_REACHABLE": Severity.INFO,
    "INCIDENT_RESOLVED": Severity.INFO,
    "INCIDENT_ESCALATION": Severity.WARNING,
    "INCIDENT_ACKED": Severity.INFO,
    "INCIDENT_SNOOZED": Severity.INFO,
    "SCREENSHOT": Severity.INFO,
    "SESSION_REPORT": Severity.INFO,
    "UNKNOWN_POPUP": Severity.WARNING,
    "TEST": Severity.INFO,
}

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def default_severity(event_type: str) -> str:
    return EVENT_TYPES.get(event_type, Severity.INFO)


@dataclass
class EvidenceRef:
    """Reference to a protected, already-redacted evidence file."""
    path: str = ""            # agent-local path (never sent raw to Telegram by the hub)
    sha256: str = ""
    size: int = 0
    captured_utc: str = ""
    kind: str = "screenshot"  # screenshot | clip
    remote_id: str = ""       # hub storage id once uploaded

    @classmethod
    def from_file(cls, path: str, captured_utc: str = "", kind: str = "screenshot") -> "EvidenceRef":
        import os
        if not path or not os.path.exists(path):
            return cls()
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return cls(path=path, sha256=h.hexdigest(), size=os.path.getsize(path), captured_utc=captured_utc, kind=kind)


@dataclass
class Event:
    device_id: str
    type: str
    summary: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = SCHEMA_VERSION
    session_id: str = ""
    incident_id: str = ""
    severity: str = ""
    category: str = ""                 # bot subscription category
    observed_utc: str = field(default_factory=utc_now_iso)
    received_utc: str = ""             # set by the hub
    owner_label: str = ""
    account: str = ""                  # observed @handle snapshot ("" when unknown)
    account_status: str = ""           # SUCCEEDED | FAILED | NOT_ATTEMPTED | DISABLED
    expected_account: str = ""
    detector: str = ""
    validity: str = "valid"            # valid | stale | unavailable
    detail: dict = field(default_factory=dict)
    evidence: EvidenceRef = field(default_factory=EvidenceRef)
    payload: dict = field(default_factory=dict)   # rendered Telegram text (caption/text/created_at)

    def __post_init__(self) -> None:
        if not self.severity:
            self.severity = default_severity(self.type)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        errors = validate_event(d)
        if errors:
            raise ValueError("invalid event: " + "; ".join(errors))
        ev = dict(d)
        ev["evidence"] = EvidenceRef(**{k: v for k, v in (ev.get("evidence") or {}).items() if k in EvidenceRef.__dataclass_fields__})
        known = {k: v for k, v in ev.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @classmethod
    def from_json(cls, text: str) -> "Event":
        return cls.from_dict(json.loads(text))


def validate_event(d: Any) -> list[str]:
    """Return a list of problems (empty when valid). Never raises."""
    errs: list[str] = []
    if not isinstance(d, dict):
        return ["event must be an object"]
    ev_id = d.get("event_id", "")
    if not isinstance(ev_id, str) or not _UUID_RE.match(ev_id):
        errs.append("event_id must be a UUID string")
    if d.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(d.get("device_id"), str) or not d.get("device_id"):
        errs.append("device_id is required")
    t = d.get("type")
    if t not in EVENT_TYPES:
        errs.append(f"unknown event type {t!r}")
    sev = d.get("severity") or default_severity(t or "")
    if sev not in SEVERITIES:
        errs.append(f"severity must be one of {SEVERITIES}")
    obs = d.get("observed_utc", "")
    if not isinstance(obs, str) or not _ISO_RE.match(obs):
        errs.append("observed_utc must be an ISO-8601 UTC timestamp")
    if not isinstance(d.get("summary", ""), str) or len(d.get("summary", "")) > 1000:
        errs.append("summary must be a string of at most 1000 characters")
    if d.get("validity", "valid") not in ("valid", "stale", "unavailable"):
        errs.append("validity must be valid | stale | unavailable")
    for key in ("detail", "payload", "evidence"):
        if key in d and d[key] is not None and not isinstance(d[key], dict):
            errs.append(f"{key} must be an object")
    try:
        if len(json.dumps(d)) > 256_000:
            errs.append("event too large (256 KB limit)")
    except (TypeError, ValueError):
        errs.append("event is not JSON-serialisable")
    return errs


def parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
