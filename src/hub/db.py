"""SQLAlchemy models. UTC timestamps are ISO-8601 strings (same as the agent
contract) so SQLite (tests, single-host) and PostgreSQL (Docker Compose) behave
identically."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Boolean, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    created_utc: Mapped[str] = mapped_column(String(40))


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)          # agent device_id (UUID)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    hostname: Mapped[str] = mapped_column(String(120), default="")
    owner_label: Mapped[str] = mapped_column(String(120), default="")
    expected_account: Mapped[str] = mapped_column(String(120), default="")
    observed_account: Mapped[str] = mapped_column(String(120), default="")
    mode: Mapped[str] = mapped_column(String(16), default="standalone")     # standalone | managed
    agent_version: Mapped[str] = mapped_column(String(40), default="")
    enrolled_utc: Mapped[str] = mapped_column(String(40), default="")
    token_hash: Mapped[str] = mapped_column(String(128), default="")
    token_salt: Mapped[str] = mapped_column(String(64), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_heartbeat_utc: Mapped[str] = mapped_column(String(40), default="")
    last_status: Mapped[dict] = mapped_column(JSON, default=dict)
    reachable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=None)   # None = never heard
    unreachable_since_utc: Mapped[str] = mapped_column(String(40), default="")
    pending_commands: Mapped[list] = mapped_column(JSON, default=list)


class PairingCode(Base):
    __tablename__ = "pairing_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_utc: Mapped[str] = mapped_column(String(40))
    expires_utc: Mapped[str] = mapped_column(String(40))
    used_utc: Mapped[str] = mapped_column(String(40), default="")
    used_by_device: Mapped[str] = mapped_column(String(36), default="")
    created_by: Mapped[str] = mapped_column(String(120), default="")


class EventRow(Base):
    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(36), index=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(48), default="")
    session_id: Mapped[str] = mapped_column(String(64), default="")
    incident_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    observed_utc: Mapped[str] = mapped_column(String(40), index=True)
    received_utc: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text, default="")
    account: Mapped[str] = mapped_column(String(120), default="")
    owner_label: Mapped[str] = mapped_column(String(120), default="")
    validity: Mapped[str] = mapped_column(String(16), default="valid")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_sha256: Mapped[str] = mapped_column(String(64), default="")
    evidence_size: Mapped[int] = mapped_column(Integer, default=0)
    evidence_stored_path: Mapped[str] = mapped_column(String(400), default="")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)   # created by the hub itself


class HeartbeatRow(Base):
    __tablename__ = "heartbeats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(36), index=True)
    received_utc: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[dict] = mapped_column(JSON, default=dict)


class IncidentRow(Base):
    __tablename__ = "incidents"
    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(36), index=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    category: Mapped[str] = mapped_column(String(48), default="")
    type: Mapped[str] = mapped_column(String(48), default="")
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    summary: Mapped[str] = mapped_column(Text, default="")
    opened_utc: Mapped[str] = mapped_column(String(40), index=True)
    resolved_utc: Mapped[str] = mapped_column(String(40), default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    acked_utc: Mapped[str] = mapped_column(String(40), default="")
    acked_by: Mapped[str] = mapped_column(String(120), default="")
    snoozed_until_utc: Mapped[str] = mapped_column(String(40), default="")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    last_event_id: Mapped[str] = mapped_column(String(36), default="")
    account: Mapped[str] = mapped_column(String(120), default="")


class Route(Base):
    """A Telegram destination. ``token_env`` names the environment variable
    holding the bot token on the hub host; the token itself is never stored."""
    __tablename__ = "routes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(120))
    token_env: Mapped[str] = mapped_column(String(120))
    chat_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(32), default="")
    categories: Mapped[list] = mapped_column(JSON, default=list)      # [] = all
    min_severity: Mapped[str] = mapped_column(String(16), default="INFO")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    commands_enabled: Mapped[bool] = mapped_column(Boolean, default=False)   # this route's bot answers /commands (hub is the single consumer)


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    route_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending | sent | failed | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_utc: Mapped[str] = mapped_column(String(40), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    sent_utc: Mapped[str] = mapped_column(String(40), default="")


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_utc: Mapped[str] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):   # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False, future=True)
