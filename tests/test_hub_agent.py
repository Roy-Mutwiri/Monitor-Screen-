"""Milestone 3 — agent side: hub outbox, client, sync loop, enrollment, managed
mode, and a true agent↔hub round trip (httpx MockTransport forwarding to the
FastAPI test client). No network."""
from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from conftest import FakeClock, FakeTransport, all_deliveries
from hub.app import create_app
from hub.config import HubSettings
from studio_monitor.app import enroll_agent, unenroll_agent
from studio_monitor.config import AppConfig
from studio_monitor.credentials import MemoryCredentialStore
from studio_monitor.hub_client import HubClient, HubClientError, hub_credential_key
from studio_monitor.hub_outbox import HubOutbox
from studio_monitor.hub_sync import HubSync
from test_activity import Harness, LIVE_TEXT

ADMIN = {"X-Admin-Token": "admin-test-token"}


class HubFixture:
    """A hub test client plus an httpx transport that forwards agent requests to it (and can go offline)."""

    def __init__(self, tmp_path, clock):
        settings = HubSettings(database_url="sqlite://", secret_key="s", admin_api_token="admin-test-token", workers_enabled=False,
                               evidence_dir=str(tmp_path / "hub-evidence"))
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
        self.telegram = FakeTransport()
        self.app = create_app(settings, engine=engine, clock=clock, telegram_transport=self.telegram)
        self.tc = TestClient(self.app)
        self.tc.__enter__()
        self.offline = False
        self.requests: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(f"{request.method} {request.url.path}")
            if self.offline:
                raise httpx.ConnectError("connection refused")
            r = self.tc.request(request.method, request.url.path, content=request.content,
                                headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")})
            return httpx.Response(r.status_code, content=r.content, headers={"content-type": r.headers.get("content-type", "")})
        return httpx.MockTransport(handler)

    def pairing_code(self) -> str:
        return self.tc.post("/api/v1/pairing-codes", json={"label": "t"}, headers=ADMIN).json()["code"]

    def devices(self):
        return self.tc.get("/api/v1/devices", headers=ADMIN).json()

    def incidents(self):
        return self.tc.get("/api/v1/incidents", headers=ADMIN).json()

    def events(self, device_id):
        from hub.db import EventRow
        with self.app.state.session_factory() as s:
            return [(e.type, e.summary, bool(e.evidence_stored_path)) for e in s.query(EventRow).filter_by(device_id=device_id).all()]


@pytest.fixture
def hubfx(tmp_path, clock):
    fx = HubFixture(tmp_path, clock)
    yield fx
    fx.tc.__exit__(None, None, None)


# ---------------------------------------------------------------- outbox

def test_outbox_backoff_and_lifecycle(tmp_path):
    clock = FakeClock()
    ob = HubOutbox(tmp_path / "o.sqlite3", clock=clock, backoff_base=5, backoff_max=40)
    e1 = {"event_id": str(uuid.uuid4()), "type": "TEST"}
    assert ob.enqueue(e1, "C:/shot.png", "abc") is True
    assert ob.enqueue(e1) is False                                   # idempotent
    assert [i.event_id for i in ob.due()] == [e1["event_id"]]
    assert ob.mark_failed([e1["event_id"]], "down") == 5
    assert ob.due() == []
    clock.advance(5); assert len(ob.due()) == 1
    assert ob.mark_failed([e1["event_id"]], "down") == 10
    for _ in range(3):
        clock.advance(100); ob.mark_failed([e1["event_id"]], "down")
    assert ob.mark_failed([e1["event_id"]], "down") == 40            # capped
    clock.advance(100)
    ob.mark_accepted(e1["event_id"], needs_evidence=True)
    assert ob.counts()["evidence"] == 1 and [i.event_id for i in ob.evidence_due()] == [e1["event_id"]]
    ob.mark_done(e1["event_id"])
    assert ob.counts()["done"] == 1
    e2 = {"event_id": str(uuid.uuid4())}
    ob.enqueue(e2); ob.mark_rejected(e2["event_id"], "bad")
    assert ob.last_error() == "bad"
    clock.advance(8 * 86400)
    assert ob.purge() == 2 and ob.counts()["done"] == 0


# ---------------------------------------------------------------- enrollment

def test_enroll_stores_secret_only_in_credential_store(hubfx, tmp_path, clock):
    cfg = AppConfig(); cfg.data_dir = str(tmp_path / "d"); cfg.machine_label = "pc1"
    path = tmp_path / "cfg.json"
    store = MemoryCredentialStore()
    res = enroll_agent(cfg, path, "http://hub.test", hubfx.pairing_code(), "managed", store=store, transport=hubfx.transport())
    assert res["mode"] == "managed" and cfg.hub.enrolled and cfg.device.device_id == res["device_id"]
    secret = store.tokens[hub_credential_key(cfg.device.device_id)]
    assert secret and secret not in path.read_text(encoding="utf-8")        # never on disk
    assert hubfx.devices()[0]["id"] == cfg.device.device_id and hubfx.devices()[0]["mode"] == "managed"
    with pytest.raises(HubClientError):
        enroll_agent(cfg, path, "http://hub.test", "BAD-CODE-XXXX", store=store, transport=hubfx.transport())
    unenroll_agent(cfg, path, store=store)
    assert not cfg.hub.enrolled and hub_credential_key(cfg.device.device_id) not in store.tokens


def test_copied_install_gets_new_identity(tmp_path):
    cfg = AppConfig(); cfg.data_dir = str(tmp_path / "d")
    first = cfg.ensure_device_id(fingerprint="fp-A")
    cfg.hub.enrolled, cfg.hub.url = True, "http://hub"
    assert cfg.ensure_device_id(fingerprint="fp-A") == first and cfg.hub.enrolled
    second = cfg.ensure_device_id(fingerprint="fp-B")                 # same files, other machine/user
    assert second != first and cfg.identity_reset and not cfg.hub.enrolled and cfg.hub.url == "http://hub"


# ---------------------------------------------------------------- sync loop

def make_sync(hubfx, clock, store, cfg, **kw) -> HubSync:
    client = HubClient("http://hub.test", cfg.device.device_id, store.tokens[hub_credential_key(cfg.device.device_id)],
                       transport=hubfx.transport())
    outbox = HubOutbox(cfg.db_path, clock=clock)
    return HubSync(client, outbox, lambda: {"live_state": "NOT_LIVE", "app_state": "RUNNING", "mode": cfg.device.mode},
                   heartbeat_seconds=15, clock=clock, mono=clock, on_event=kw.get("on_event"))


def managed_harness(hubfx, cfg, rules, clock):
    store = MemoryCredentialStore()
    enroll_agent(cfg, cfg.data_path / "cfg.json", "http://hub.test", hubfx.pairing_code(), "managed", store=store,
                 transport=hubfx.transport())
    h = Harness(cfg, rules, clock)
    sync = make_sync(hubfx, clock, store, cfg, on_event=h.events.append)
    h.mon.hub_sync = sync
    sync.status_provider = h.mon.hub_status_payload
    return h, sync


def test_managed_mode_mirrors_events_to_hub_and_withholds_local_delivery(hubfx, cfg, rules, clock):
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    h, sync = managed_harness(hubfx, cfg, rules, clock)
    h.run(10)
    assert sync.status.connected and hubfx.devices()[0]["status"] == "STUDIO_OPEN"
    # a restriction popup: recorded locally, NOT delivered by local bots (managed), mirrored to the hub with evidence
    h.ocr.default = "Your LIVE was ended due to a violation of our Community Guidelines"
    h.run(6)
    local = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"]
    assert local == []                                               # hub owns delivery in managed mode
    types = [t for t, _s, _e in hubfx.events(cfg.device.device_id)]
    assert "STUDIO_ALREADY_RUNNING" in types and any(t in ("LIVE_INTERRUPTED", "RESTRICTION") for t in types)
    incs = hubfx.incidents()
    assert len(incs) == 1 and incs[0]["device_id"] == cfg.device.device_id
    assert any(e for t, _s, e in hubfx.events(cfg.device.device_id) if t in ("LIVE_INTERRUPTED", "RESTRICTION") and e)   # evidence uploaded
    assert sync.outbox.counts()["pending"] == 0 and sync.status.rejected == 0
    # popup disappears -> resolution mirrored, hub incident closed
    h.ocr.default = "Scenes Sources Go LIVE"
    h.run(60)
    assert hubfx.incidents() == []
    assert "INCIDENT_RESOLVED" in [t for t, _s, _e in hubfx.events(cfg.device.device_id)]


def test_offline_hub_buffers_then_drains_without_duplicates(hubfx, cfg, rules, clock):
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    h, sync = managed_harness(hubfx, cfg, rules, clock)
    h.run(4)
    hubfx.offline = True
    before = len(hubfx.events(cfg.device.device_id))
    h.ocr.default = "Your LIVE was ended due to a violation of our Community Guidelines"
    h.run(120)
    assert sync.outbox.counts()["pending"] >= 1 and not sync.status.connected and "hub unreachable" in sync.status.last_error
    hubfx.offline = False
    h.run(120)
    assert sync.outbox.counts()["pending"] == 0 and sync.status.connected
    after = hubfx.events(cfg.device.device_id)
    assert len(after) > before
    # the batch was retried several times while offline; the hub holds each event exactly once
    from hub.db import EventRow
    with hubfx.app.state.session_factory() as s:
        rows = s.query(EventRow).filter_by(device_id=cfg.device.device_id).all()
    assert len({r.event_id for r in rows}) == len(rows) == len(after)
    assert sum(1 for r in rows if r.type == "LIVE_INTERRUPTED") == 1
    assert sum(1 for r in hubfx.requests if r.endswith("/api/v1/events")) >= 2    # offline retries actually happened


def test_standalone_enrolled_device_still_delivers_locally(hubfx, cfg, rules, clock):
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    store = MemoryCredentialStore()
    enroll_agent(cfg, cfg.data_path / "cfg.json", "http://hub.test", hubfx.pairing_code(), "standalone", store=store,
                 transport=hubfx.transport())
    h = Harness(cfg, rules, clock)
    sync = make_sync(hubfx, clock, store, cfg)
    h.mon.hub_sync = sync
    sync.status_provider = h.mon.hub_status_payload
    h.ocr.default = "Your LIVE was ended due to a violation of our Community Guidelines"
    h.run(10)
    assert [d for d in all_deliveries(h.queue) if d["kind"] == "incident"]      # local bots still deliver
    assert hubfx.incidents()                                                     # and the hub mirrors the incident
    from hub.db import DeliveryRow
    with hubfx.app.state.session_factory() as s:
        assert s.query(DeliveryRow).count() == 0                                # hub does not route standalone devices


def test_revoked_credential_stops_sync_cleanly(hubfx, cfg, rules, clock):
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    h, sync = managed_harness(hubfx, cfg, rules, clock)
    h.run(4)
    hubfx.tc.post(f"/api/v1/devices/{cfg.device.device_id}/revoke", headers=ADMIN)
    h.ocr.default = "Your LIVE was ended due to a violation of our Community Guidelines"
    h.run(40)
    assert not sync.status.connected and "401" in sync.status.last_error
    assert sync.outbox.counts()["pending"] >= 1                                # kept for a later re-enrollment, not dropped


def test_hub_status_payload_and_activity_snapshot(hubfx, cfg, rules, clock):
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    h, sync = managed_harness(hubfx, cfg, rules, clock)
    h.ocr.default = LIVE_TEXT
    h.run(40)
    payload = h.mon.hub_status_payload()
    assert payload["live_state"] == "LIVE" and payload["mode"] == "managed" and payload["version"]
    assert h.mon.activity.hub["connected"] is True and h.mon.activity.hub["pending"] == 0
    assert hubfx.devices()[0]["status"] == "LIVE"
