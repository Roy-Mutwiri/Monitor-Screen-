"""Milestone 3 — hub: enrollment, authenticated ingestion with dedup,
heartbeats / unreachable sweep, incident mirror, Telegram routing, dashboard.
SQLite in-memory engine, FakeClock, FakeTransport (no network)."""
from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.pool import StaticPool

from conftest import FakeClock, FakeTransport
from hub.app import create_app
from hub.config import HubSettings, hash_password, verify_password
from hub.db import make_engine
from studio_monitor.contracts.events import Event, Severity

ADMIN = {"X-Admin-Token": "admin-test-token"}
DEVICE_ID = str(uuid.uuid4())


@pytest.fixture
def hub(tmp_path):
    clock = FakeClock(1_700_000_000.0)
    transport = FakeTransport()
    settings = HubSettings(database_url="sqlite://", secret_key="test-secret", admin_api_token="admin-test-token",
                           workers_enabled=False, evidence_dir=str(tmp_path / "evidence"), pairing_ttl_seconds=600)
    from sqlalchemy import create_engine
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    app = create_app(settings, engine=engine, clock=clock, telegram_transport=transport)
    with TestClient(app) as client:
        client.clock, client.transport, client.app_ = clock, transport, app
        yield client


def pairing_code(hub, label="pc-1", ttl=None):
    body = {"label": label}
    if ttl is not None:
        body["ttl_seconds"] = ttl
    r = hub.post("/api/v1/pairing-codes", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def enroll(hub, code=None, device_id=DEVICE_ID, mode="managed", **extra):
    code = code or pairing_code(hub)
    r = hub.post("/api/v1/enroll", json={"code": code, "device_id": device_id, "name": "Roy's PC", "hostname": "DESKTOP",
                                         "agent_version": "1.0.0", "mode": mode, **extra})
    return r


def auth(enrolled: dict) -> dict:
    return {"Authorization": f"Bearer {enrolled['device_id']}:{enrolled['secret']}"}


def make_event(device_id=DEVICE_ID, type_="RESTRICTION", incident_id="INC-1", **kw) -> dict:
    ev = Event(device_id=device_id, type=type_, summary=kw.pop("summary", "Restriction notice: Your LIVE was ended"),
               incident_id=incident_id, category=kw.pop("category", "restrictions"),
               observed_utc="2026-10-09T12:00:00+00:00", payload={"text": "hello", "caption": "hello"}, **kw)
    return ev.to_dict()


# ---------------------------------------------------------------- enrollment / auth

def test_pairing_code_is_single_use_and_expires(hub):
    code = pairing_code(hub)
    assert enroll(hub, code).status_code == 200
    r = enroll(hub, code, device_id=str(uuid.uuid4()))
    assert r.status_code == 403 and "already used" in r.text
    assert enroll(hub, "ZZZZ-ZZZZ-ZZZZ", device_id=str(uuid.uuid4())).status_code == 403
    code2 = pairing_code(hub, ttl=60)
    hub.clock.advance(61)
    r = enroll(hub, code2, device_id=str(uuid.uuid4()))
    assert r.status_code == 403 and "expired" in r.text


def test_admin_api_requires_token(hub):
    assert hub.post("/api/v1/pairing-codes", json={}).status_code == 401
    assert hub.get("/api/v1/devices").status_code == 401
    assert hub.post("/api/v1/pairing-codes", json={}, headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_agent_credential_is_checked(hub):
    enrolled = enroll(hub).json()
    assert hub.post("/api/v1/heartbeat", json={"status": {}}).status_code == 401
    bad = {"Authorization": f"Bearer {DEVICE_ID}:nope"}
    assert hub.post("/api/v1/heartbeat", json={"status": {}}, headers=bad).status_code == 401
    assert hub.post("/api/v1/heartbeat", json={"status": {}}, headers=auth(enrolled)).status_code == 200
    hub.post(f"/api/v1/devices/{DEVICE_ID}/revoke", headers=ADMIN)
    assert hub.post("/api/v1/heartbeat", json={"status": {}}, headers=auth(enrolled)).status_code == 401


def test_re_enrollment_rotates_secret(hub):
    first = enroll(hub).json()
    second = enroll(hub, device_id=DEVICE_ID).json()
    assert first["secret"] != second["secret"]
    assert hub.post("/api/v1/heartbeat", json={"status": {}}, headers=auth(first)).status_code == 401
    assert hub.post("/api/v1/heartbeat", json={"status": {}}, headers=auth(second)).status_code == 200


# ---------------------------------------------------------------- ingestion

def test_ingest_dedup_scope_and_validation(hub):
    enrolled = enroll(hub).json()
    e1 = make_event()
    other = make_event(device_id=str(uuid.uuid4()), incident_id="INC-9")
    bad = {"event_id": "not-a-uuid"}
    r = hub.post("/api/v1/events", json=[e1, e1, other, bad], headers=auth(enrolled))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["accepted"] == [e1["event_id"]] and res["duplicate"] == [e1["event_id"]]
    assert "does not match" in res["rejected"][other["event_id"]] and "not-a-uuid" in res["rejected"]
    # at-least-once re-upload of the same batch is a no-op
    res2 = hub.post("/api/v1/events", json=[e1], headers=auth(enrolled)).json()
    assert res2["accepted"] == [] and res2["duplicate"] == [e1["event_id"]]
    incs = hub.get("/api/v1/incidents", headers=ADMIN).json()
    assert len(incs) == 1 and incs[0]["incident_id"] == "INC-1" and incs[0]["severity"] == Severity.URGENT


def test_incident_mirror_occurrence_and_resolution(hub):
    enrolled = enroll(hub).json()
    hub.post("/api/v1/events", json=[make_event(), make_event(summary="again")], headers=auth(enrolled))
    incs = hub.get("/api/v1/incidents", headers=ADMIN).json()
    assert incs[0]["occurrences"] == 2
    res = make_event(type_="INCIDENT_RESOLVED", summary="Restriction notice no longer visible")
    hub.post("/api/v1/events", json=[res], headers=auth(enrolled))
    assert hub.get("/api/v1/incidents", headers=ADMIN).json() == []
    # operator actions
    hub.post("/api/v1/events", json=[make_event(incident_id="INC-2")], headers=auth(enrolled))
    r = hub.post("/api/v1/incidents/INC-2/ack", headers=ADMIN)
    assert r.status_code == 200 and r.json()["acked_by"] == "api-token"
    r = hub.post("/api/v1/incidents/INC-2/snooze", json={"seconds": 600}, headers=ADMIN)
    assert r.json()["snoozed_until_utc"]
    r = hub.post("/api/v1/incidents/INC-2/resolve", json={"text": "handled"}, headers=ADMIN)
    assert r.json()["resolved_utc"]
    assert hub.post("/api/v1/incidents/NOPE/ack", headers=ADMIN).status_code == 404


# ---------------------------------------------------------------- heartbeats / reachability

def add_route(hub, name="ops", categories=None, min_severity="INFO", token_env="HUB_BOT_OPS"):
    r = hub.post("/api/v1/routes", json={"name": name, "token_env": token_env, "chat_id": "42",
                                         "categories": categories or [], "min_severity": min_severity}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def sent_texts(hub):
    import json as _json
    from urllib.parse import parse_qs
    out = []
    for url, data, _h in hub.transport.requests:
        if data is None:
            continue
        if url.endswith("/sendMessage"):
            out.append(parse_qs(data.decode())["text"][0])
        elif url.endswith("/sendPhoto"):
            out.append("PHOTO:" + data.decode(errors="ignore")[:200])
    return out


def test_heartbeat_unreachable_and_recovery_route_to_telegram(hub, monkeypatch):
    enrolled = enroll(hub).json()
    add_route(hub)
    hub.app_.state.worker.env = {"HUB_BOT_OPS": "123456789:AAFakeTokenForTests_abcdefghijklmnop"}
    hub.post("/api/v1/heartbeat", json={"status": {"live_state": "LIVE", "app_state": "RUNNING"}}, headers=auth(enrolled))
    devs = hub.get("/api/v1/devices", headers=ADMIN).json()
    assert devs[0]["status"] == "LIVE" and devs[0]["reachable"] is True
    hub.clock.advance(60)
    assert hub.post("/api/v1/admin/sweep", headers=ADMIN).json()["unreachable"] == 0     # 60 s < 90 s
    hub.clock.advance(40)
    out = hub.post("/api/v1/admin/sweep", headers=ADMIN).json()
    assert out["unreachable"] == 1 and out["delivered"] == 1
    incs = hub.get("/api/v1/incidents", headers=ADMIN).json()
    assert len(incs) == 1 and incs[0]["type"] == "DEVICE_UNREACHABLE"
    texts = sent_texts(hub)
    assert len(texts) == 1 and "Device unreachable" in texts[0] and "heartbeat missing" in texts[0]
    hub.clock.advance(300)
    out = hub.post("/api/v1/admin/sweep", headers=ADMIN).json()
    assert out["unreachable"] == 0 and out["delivered"] == 0                         # no duplicate while still down
    assert hub.get("/api/v1/devices", headers=ADMIN).json()[0]["status"] == "UNREACHABLE"
    r = hub.post("/api/v1/heartbeat", json={"status": {"app_state": "RUNNING"}}, headers=auth(enrolled))
    assert r.status_code == 200 and r.json()["commands"] == []
    assert hub.get("/api/v1/incidents", headers=ADMIN).json() == []
    hub.post("/api/v1/admin/sweep", headers=ADMIN)
    texts = sent_texts(hub)
    assert len(texts) == 2 and "reachable again" in texts[1]


def test_routing_only_for_managed_devices_with_filters(hub):
    managed = enroll(hub).json()
    standalone_id = str(uuid.uuid4())
    standalone = enroll(hub, device_id=standalone_id, mode="standalone").json()
    add_route(hub, "urgent-only", min_severity="URGENT")
    add_route(hub, "health", categories=["health"])
    hub.app_.state.worker.env = {"HUB_BOT_OPS": "123456789:AAFakeTokenForTests_abcdefghijklmnop"}
    r = hub.post("/api/v1/events", json=[make_event(), make_event(type_="STUDIO_OPENED", incident_id="", category="studio_opened",
                                                                 summary="Studio opened")], headers=auth(managed))
    assert r.json()["routed"] == 1                     # URGENT restriction -> urgent-only route; INFO opened -> none
    r = hub.post("/api/v1/events", json=[make_event(device_id=standalone_id, incident_id="INC-S")], headers=auth(standalone))
    assert r.json()["routed"] == 0                     # standalone devices deliver locally; hub does not duplicate
    assert hub.post("/api/v1/admin/sweep", headers=ADMIN).json()["delivered"] == 1
    assert len(sent_texts(hub)) == 1


def test_snoozed_incident_is_not_routed_but_resolution_is(hub):
    managed = enroll(hub).json()
    add_route(hub)
    hub.app_.state.worker.env = {"HUB_BOT_OPS": "123456789:AAFakeTokenForTests_abcdefghijklmnop"}
    hub.post("/api/v1/events", json=[make_event()], headers=auth(managed))
    hub.post("/api/v1/incidents/INC-1/snooze", json={"seconds": 3600}, headers=ADMIN)
    r = hub.post("/api/v1/events", json=[make_event(summary="repeat")], headers=auth(managed))
    assert r.json()["routed"] == 0
    r = hub.post("/api/v1/events", json=[make_event(type_="INCIDENT_RESOLVED", summary="gone")], headers=auth(managed))
    assert r.json()["routed"] == 1


def test_missing_token_env_backs_off_instead_of_dying(hub):
    managed = enroll(hub).json()
    add_route(hub, token_env="HUB_BOT_MISSING")
    hub.app_.state.worker.env = {}
    hub.post("/api/v1/events", json=[make_event()], headers=auth(managed))
    hub.post("/api/v1/admin/sweep", headers=ADMIN)
    from hub.db import DeliveryRow
    with hub.app_.state.session_factory() as s:
        d = s.query(DeliveryRow).one()
        assert d.status == "failed" and "not set on the hub" in d.last_error and d.attempts == 1
    assert sent_texts(hub) == []


def test_telegram_error_marks_permanent_failures_dead(hub):
    managed = enroll(hub).json()
    add_route(hub)
    hub.app_.state.worker.env = {"HUB_BOT_OPS": "123456789:AAFakeTokenForTests_abcdefghijklmnop"}
    hub.transport.responses.append((403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}))
    hub.post("/api/v1/events", json=[make_event()], headers=auth(managed))
    hub.post("/api/v1/admin/sweep", headers=ADMIN)
    from hub.db import DeliveryRow
    with hub.app_.state.session_factory() as s:
        d = s.query(DeliveryRow).one()
        assert d.status == "dead" and "blocked" in d.last_error


# ---------------------------------------------------------------- evidence

def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_evidence_upload_checks_sha_and_device(hub, tmp_path):
    import hashlib
    enrolled = enroll(hub).json()
    data = _png()
    ev = make_event()
    ev["evidence"] = {"path": "C:/x.png", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "kind": "screenshot"}
    hub.post("/api/v1/events", json=[ev], headers=auth(enrolled))
    r = hub.post(f"/api/v1/events/{ev['event_id']}/evidence", files={"file": ("x.png", b"\x89PNG" + b"junk", "image/png")},
                 headers=auth(enrolled))
    assert r.status_code == 400 and "sha256" in r.text
    r = hub.post(f"/api/v1/events/{ev['event_id']}/evidence", files={"file": ("x.png", data, "image/png")}, headers=auth(enrolled))
    assert r.status_code == 200 and r.json()["stored"] is True
    assert hub.post(f"/api/v1/events/{uuid.uuid4()}/evidence", files={"file": ("x.png", data, "image/png")},
                    headers=auth(enrolled)).status_code == 404
    page = hub.get(f"/devices/{DEVICE_ID}")
    assert page.status_code == 200 and "stored" in page.text


# ---------------------------------------------------------------- dashboard

def test_dashboard_pages_render_and_login_gate(hub):
    enrolled = enroll(hub).json()
    hub.post("/api/v1/heartbeat", json={"status": {"live_state": "NOT_LIVE", "app_state": "RUNNING"}}, headers=auth(enrolled))
    hub.post("/api/v1/events", json=[make_event()], headers=auth(enrolled))
    for path in ("/", "/incidents", "/events", f"/devices/{DEVICE_ID}", "/login", "/api/v1/health"):
        assert hub.get(path).status_code == 200, path
    assert "Roy&#39;s PC" in hub.get("/").text or "Roy's PC" in hub.get("/").text
    assert "INC-1" in hub.get("/incidents").text
    # with a password the pages redirect to /login until signed in
    hub.app_.state.settings.admin_password_hash = hash_password("hunter2")
    r = hub.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Wrong password" in hub.post("/login", data={"password": "nope"}).text
    r = hub.post("/login", data={"password": "hunter2"}, follow_redirects=False)
    assert r.status_code == 303 and "hub_session" in r.headers.get("set-cookie", "")
    assert hub.get("/").status_code == 200


def test_password_hashing_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h) and not verify_password("other", h) and not verify_password("x", "garbage")
    assert HubSettings.from_env({"HUB_ADMIN_PASSWORD": "pw", "HUB_UNREACHABLE_AFTER": "120"}).unreachable_after == 120
