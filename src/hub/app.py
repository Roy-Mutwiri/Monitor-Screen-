"""FastAPI application: agent API (enroll, events, evidence, heartbeat),
admin API (pairing codes, devices, incidents, routes) and the HTML dashboard.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from studio_monitor.contracts.events import SCHEMA_VERSION

from . import __version__
from .config import HubSettings, verify_password
from .db import Device, make_engine, make_session_factory
from .delivery import HubBackground, HubDeliveryWorker
from .services import HubError, HubService

log = logging.getLogger("hub")
TEMPLATES = Path(__file__).parent / "templates"
PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47])
JPEG_MAGIC = bytes([0xFF, 0xD8])
GIF_MAGIC = b"GIF8"


# ---------------------------------------------------------------- request models
class EnrollRequest(BaseModel):
    code: str = Field(min_length=4, max_length=40)
    device_id: str
    name: str = ""
    hostname: str = ""
    agent_version: str = ""
    mode: str = "standalone"
    owner_label: str = ""
    expected_account: str = ""


class HeartbeatRequest(BaseModel):
    status: dict = Field(default_factory=dict)


class PairingRequest(BaseModel):
    workspace: str = ""
    label: str = ""
    ttl_seconds: Optional[int] = None


class RouteRequest(BaseModel):
    workspace: str = ""
    name: str
    token_env: str
    chat_id: str
    thread_id: str = ""
    categories: list[str] = Field(default_factory=list)
    min_severity: str = "INFO"
    commands_enabled: bool = False


class IncidentAction(BaseModel):
    text: str = ""
    seconds: int = 1800


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    owner_label: Optional[str] = None
    expected_account: Optional[str] = None
    mode: Optional[str] = None


# ---------------------------------------------------------------- app factory
def create_app(settings: Optional[HubSettings] = None, engine=None, clock=None, telegram_transport=None,
               memory_client=None) -> FastAPI:
    settings = settings or HubSettings.from_env()
    engine = engine or make_engine(settings.database_url)
    from .memory import HubMemory, MemorySyncRow  # noqa: F401  (registers the table before create_all)
    session_factory = make_session_factory(engine)
    signer = URLSafeTimedSerializer(settings.secret_key, salt="hub-session")
    worker = HubDeliveryWorker(session_factory, settings.telegram_api_base, transport=telegram_transport,
                               clock=clock or __import__("time").time, evidence_dir=settings.evidence_dir)
    background = HubBackground(session_factory, worker, settings.unreachable_after, retention_days=settings.retention_days,
                               clock=clock or __import__("time").time)
    from .commands import HubCommandPollers
    background.commands = HubCommandPollers(session_factory, worker.env, settings.telegram_api_base, telegram_transport,
                                            clock or __import__("time").time, settings.evidence_dir)
    background.memory = HubMemory(session_factory, settings.supermemory_api_key, settings.supermemory_namespace_prefix, client=memory_client)
    background.commands.memory = background.memory
    app_memory = background.memory
    templates = Jinja2Templates(directory=str(TEMPLATES))

    with session_factory() as s:
        HubService(s, clock).ensure_workspace(settings.default_workspace)
        s.commit()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.workers_enabled:
            background.start()
        try:
            yield
        finally:
            background.stop()

    app = FastAPI(title="Studio Monitor Hub", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.settings, app.state.session_factory, app.state.background, app.state.worker = settings, session_factory, background, worker

    # ------------------------------------------------------------ dependencies
    def db():
        with session_factory() as s:
            yield s

    def svc(s: Session = Depends(db)) -> HubService:
        return HubService(s, clock, settings.pairing_ttl_seconds, settings.unreachable_after)

    def agent(authorization: str = Header(default=""), service: HubService = Depends(svc)) -> Device:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "agent credential required")
        cred = authorization[7:].strip()
        if ":" not in cred:
            raise HTTPException(401, "malformed agent credential")
        device_id, secret = cred.split(":", 1)
        dev = service.authenticate(device_id, secret)
        if dev is None:
            raise HTTPException(401, "invalid or revoked agent credential")
        return dev

    def admin_actor(request: Request, x_admin_token: str = Header(default="")) -> str:
        if settings.admin_api_token and x_admin_token and secrets.compare_digest(x_admin_token, settings.admin_api_token):
            return "api-token"
        cookie = request.cookies.get("hub_session", "")
        if cookie:
            try:
                data = signer.loads(cookie, max_age=12 * 3600)
                return str(data.get("user", "admin"))
            except BadSignature:
                pass
        raise HTTPException(401, "admin authentication required")

    def page_actor(request: Request) -> Optional[str]:
        cookie = request.cookies.get("hub_session", "")
        if not cookie:
            return None
        try:
            return str(signer.loads(cookie, max_age=12 * 3600).get("user", "admin"))
        except BadSignature:
            return None

    def commit(s: Session, fn):
        try:
            out = fn()
            s.commit()
            return out
        except HubError as exc:
            s.rollback()
            raise HTTPException(exc.status, str(exc))

    # ------------------------------------------------------------ agent API
    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "version": __version__, "schema_version": SCHEMA_VERSION}

    @app.post("/api/v1/enroll")
    def enroll(req: EnrollRequest, s: Session = Depends(db), service: HubService = Depends(svc)):
        dev, secret = commit(s, lambda: service.enroll(req.code, req.device_id, req.name, req.hostname, req.agent_version,
                                                       req.mode, req.owner_label, req.expected_account))
        return {"device_id": dev.id, "secret": secret, "workspace_id": dev.workspace_id,
                "heartbeat_interval": settings.heartbeat_interval, "unreachable_after": settings.unreachable_after}

    @app.post("/api/v1/events")
    def post_events(events: list[dict], dev: Device = Depends(agent), s: Session = Depends(db), service: HubService = Depends(svc)):
        dev = s.merge(dev)
        return commit(s, lambda: service.ingest(dev, events).to_dict())

    @app.post("/api/v1/events/{event_id}/evidence")
    async def upload_evidence(event_id: str, file: UploadFile = File(...), sha256: str = Form(""),
                              dev: Device = Depends(agent), s: Session = Depends(db), service: HubService = Depends(svc)):
        from .db import EventRow
        row = s.get(EventRow, event_id)
        if row is None or row.device_id != dev.id:
            raise HTTPException(404, "unknown event for this device")
        data = await file.read(settings.evidence_max_bytes + 1)
        if len(data) > settings.evidence_max_bytes:
            raise HTTPException(413, "evidence too large")
        digest = hashlib.sha256(data).hexdigest()
        expected = sha256 or row.evidence_sha256
        if expected and digest != expected:
            raise HTTPException(400, "sha256 mismatch")
        if data[:4] != PNG_MAGIC and data[:2] != JPEG_MAGIC and data[:4] != GIF_MAGIC:
            raise HTTPException(415, "only PNG, JPEG or GIF evidence is accepted")
        dest = Path(settings.evidence_dir) / dev.workspace_id / dev.id
        dest.mkdir(parents=True, exist_ok=True)
        ext = ".png" if data[:4] == PNG_MAGIC else (".gif" if data[:4] == GIF_MAGIC else ".jpg")
        path = dest / f"{event_id}{ext}"
        path.write_bytes(data)
        row.evidence_stored_path, row.evidence_sha256, row.evidence_size = str(path), digest, len(data)
        s.commit()
        return {"ok": True, "stored": True, "sha256": digest}

    @app.post("/api/v1/heartbeat")
    def heartbeat(req: HeartbeatRequest, dev: Device = Depends(agent), s: Session = Depends(db), service: HubService = Depends(svc)):
        dev = s.merge(dev)
        return commit(s, lambda: service.heartbeat(dev, req.status))

    # ------------------------------------------------------------ admin API
    @app.post("/api/v1/pairing-codes")
    def pairing(req: PairingRequest, actor: str = Depends(admin_actor), s: Session = Depends(db), service: HubService = Depends(svc)):
        ws = service.ensure_workspace(req.workspace or settings.default_workspace)
        code, row = commit(s, lambda: service.create_pairing_code(ws.id, req.label, actor, req.ttl_seconds))
        return {"code": code, "expires_utc": row.expires_utc, "workspace_id": ws.id}

    @app.get("/api/v1/devices")
    def devices(actor: str = Depends(admin_actor), service: HubService = Depends(svc)):
        return [_device_dict(service, d) for d in service.devices()]

    @app.patch("/api/v1/devices/{device_id}")
    def update_device(device_id: str, req: DeviceUpdate, actor: str = Depends(admin_actor), s: Session = Depends(db),
                      service: HubService = Depends(svc)):
        dev = commit(s, lambda: service.update_device(device_id, actor, **req.model_dump()))
        return _device_dict(service, dev)

    @app.post("/api/v1/devices/{device_id}/revoke")
    def revoke(device_id: str, actor: str = Depends(admin_actor), s: Session = Depends(db), service: HubService = Depends(svc)):
        commit(s, lambda: service.revoke(device_id, actor))
        return {"ok": True}

    @app.get("/api/v1/incidents")
    def incidents(actor: str = Depends(admin_actor), service: HubService = Depends(svc)):
        return [_incident_dict(i) for i in service.open_incidents()]

    @app.post("/api/v1/incidents/{incident_id}/ack")
    def ack(incident_id: str, actor: str = Depends(admin_actor), s: Session = Depends(db), service: HubService = Depends(svc)):
        return _incident_dict(commit(s, lambda: service.ack(incident_id, actor)))

    @app.post("/api/v1/incidents/{incident_id}/snooze")
    def snooze(incident_id: str, req: IncidentAction, actor: str = Depends(admin_actor), s: Session = Depends(db),
               service: HubService = Depends(svc)):
        return _incident_dict(commit(s, lambda: service.snooze(incident_id, req.seconds, actor)))

    @app.post("/api/v1/incidents/{incident_id}/resolve")
    def resolve(incident_id: str, req: IncidentAction, actor: str = Depends(admin_actor), s: Session = Depends(db),
                service: HubService = Depends(svc)):
        return _incident_dict(commit(s, lambda: service.resolve(incident_id, req.text or "resolved by operator", actor)))

    @app.post("/api/v1/routes")
    def add_route(req: RouteRequest, actor: str = Depends(admin_actor), s: Session = Depends(db), service: HubService = Depends(svc)):
        ws = service.ensure_workspace(req.workspace or settings.default_workspace)
        r = commit(s, lambda: service.add_route(ws.id, req.name, req.token_env, req.chat_id, req.thread_id, req.categories,
                                                req.min_severity, actor, req.commands_enabled))
        return {"id": r.id, "name": r.name, "token_env": r.token_env, "token_configured": bool(os.environ.get(r.token_env))}

    @app.post("/api/v1/admin/sweep")
    def sweep(actor: str = Depends(admin_actor)):
        return background.once()

    @app.get("/api/v1/memory/search")
    def memory_search(q: str, device_id: str = "", workspace: str = "", limit: int = 5, actor: str = Depends(admin_actor),
                      service: HubService = Depends(svc)):
        if not app_memory.configured:
            return {"configured": False, "note": "SUPERMEMORY_API_KEY is not set on the hub", "hits": []}
        ws = service.ensure_workspace(workspace or settings.default_workspace)
        hits = app_memory.search(ws.id, q, device_id, max(1, min(limit, 20)))
        return {"configured": True, "note": "retrieved text is reference only, not verified now",
                "hits": [{"id": h.id, "text": h.text, "similarity": h.similarity, "metadata": h.metadata} for h in hits]}

    @app.post("/api/v1/admin/memory-sync")
    def memory_sync(actor: str = Depends(admin_actor)):
        from .services import now_iso
        return app_memory.sync_once(now_iso(clock))

    # ------------------------------------------------------------ dashboard
    def render(request: Request, name: str, **ctx):
        ctx.update(request=request, version=__version__, actor=page_actor(request))
        return templates.TemplateResponse(request, name, ctx)

    def require_page_login(request: Request):
        if settings.admin_password_hash and page_actor(request) is None:
            return RedirectResponse("/login", status_code=303)
        return None

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "login.html", error="", login_enabled=bool(settings.admin_password_hash))

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        if not settings.admin_password_hash or not verify_password(password, settings.admin_password_hash):
            return render(request, "login.html", error="Wrong password", login_enabled=bool(settings.admin_password_hash))
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("hub_session", signer.dumps({"user": "admin"}), httponly=True, samesite="lax",
                        secure=settings.public_url.startswith("https"))
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("hub_session")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, service: HubService = Depends(svc)):
        if (r := require_page_login(request)) is not None:
            return r
        devs = service.devices()
        return render(request, "devices.html", devices=[_device_dict(service, d) for d in devs], counts=service.counts(),
                      incidents=[_incident_dict(i) for i in service.open_incidents()[:20]])

    @app.get("/devices/{device_id}", response_class=HTMLResponse)
    def device_page(request: Request, device_id: str, service: HubService = Depends(svc)):
        if (r := require_page_login(request)) is not None:
            return r
        dev = service.s.get(Device, device_id)
        if dev is None:
            raise HTTPException(404)
        return render(request, "device.html", device=_device_dict(service, dev),
                      events=[_event_dict(e) for e in service.recent_events(device_id, 100)],
                      incidents=[_incident_dict(i) for i in service.open_incidents() if i.device_id == device_id])

    @app.get("/incidents", response_class=HTMLResponse)
    def incidents_page(request: Request, service: HubService = Depends(svc)):
        if (r := require_page_login(request)) is not None:
            return r
        return render(request, "incidents.html", incidents=[_incident_dict(i) for i in service.open_incidents()])

    @app.get("/events", response_class=HTMLResponse)
    def events_page(request: Request, service: HubService = Depends(svc)):
        if (r := require_page_login(request)) is not None:
            return r
        return render(request, "events.html", events=[_event_dict(e) for e in service.recent_events(None, 200)])

    @app.exception_handler(HubError)
    async def hub_error(_request, exc: HubError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    return app


def _device_dict(service: HubService, d: Device) -> dict:
    st = d.last_status or {}
    return {"id": d.id, "name": d.name or d.hostname or d.id[:8], "hostname": d.hostname, "owner_label": d.owner_label,
            "expected_account": d.expected_account, "observed_account": d.observed_account, "mode": d.mode,
            "agent_version": d.agent_version, "enrolled_utc": d.enrolled_utc, "last_heartbeat_utc": d.last_heartbeat_utc,
            "status": service.device_status(d), "reachable": d.reachable, "revoked": d.revoked,
            "live_state": st.get("live_state", ""), "app_state": st.get("app_state", ""), "capture": st.get("capture", ""),
            "pending": st.get("pending", 0), "workspace_id": d.workspace_id,
            "account_mismatch": bool(d.expected_account and d.observed_account and
                                     d.expected_account.lstrip("@").lower() != d.observed_account.lstrip("@").lower())}


def _incident_dict(i) -> dict:
    return {"incident_id": i.incident_id, "device_id": i.device_id, "category": i.category, "type": i.type, "severity": i.severity,
            "summary": i.summary, "opened_utc": i.opened_utc, "resolved_utc": i.resolved_utc, "acked_utc": i.acked_utc,
            "acked_by": i.acked_by, "snoozed_until_utc": i.snoozed_until_utc, "occurrences": i.occurrences, "account": i.account}


def _event_dict(e) -> dict:
    return {"event_id": e.event_id, "device_id": e.device_id, "type": e.type, "severity": e.severity, "category": e.category,
            "observed_utc": e.observed_utc, "received_utc": e.received_utc, "summary": e.summary, "account": e.account,
            "incident_id": e.incident_id, "evidence": bool(e.evidence_stored_path), "synthetic": e.synthetic}
