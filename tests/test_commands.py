"""Milestone 4 — Telegram commands, inline buttons, single update consumer,
escalation route, e-mail backup, hub-side commands and the predefined remote
screenshot operation. Scripted Telegram transport; no network."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from conftest import TOKEN_A, FakeClock, FakeTransport, all_deliveries
from studio_monitor.commands import CommandRouter, UpdatePoller, incident_keyboard, parse_command
from studio_monitor.config import TelegramConfig
from studio_monitor.email_backup import EmailBackup, SmtpSettings
from studio_monitor.queue import DeliveryError
from studio_monitor.telegram import ClientFactory, TelegramClient
from test_activity import Harness, LIVE_TEXT, NOT_LIVE_TEXT

RESTRICTION = "Your LIVE was ended due to a violation of our Community Guidelines"


class TgTransport:
    """Telegram transport that answers getUpdates from a script and records everything else."""

    def __init__(self):
        self.updates: list[list[dict]] = []
        self.sent: list[tuple[str, dict]] = []     # (method, fields)
        self.fail_get_updates: Exception | None = None
        self.send_status = 200

    def __call__(self, url, data, headers, timeout):
        method = url.rsplit("/", 1)[1]
        if data is not None and headers.get("Content-Type", "").startswith("application/x-www-form"):
            fields = {k: v[0] for k, v in parse_qs(data.decode()).items()}
        else:
            fields = {"_multipart": True, "_size": len(data or b"")}
        if method == "getUpdates":
            if self.fail_get_updates is not None:
                exc, self.fail_get_updates = self.fail_get_updates, None
                raise exc
            batch = self.updates.pop(0) if self.updates else []
            return 200, json.dumps({"ok": True, "result": batch}).encode()
        self.sent.append((method, fields))
        if self.send_status != 200:
            return self.send_status, json.dumps({"ok": False, "description": "Forbidden: bot was blocked by the user"}).encode()
        return 200, json.dumps({"ok": True, "result": {"message_id": len(self.sent)}}).encode()

    def texts(self):
        return [f.get("text", "") for m, f in self.sent if m == "sendMessage"]


def msg(update_id, text, chat="42", user="roy", mid=100):
    return {"update_id": update_id, "message": {"message_id": mid, "text": text, "chat": {"id": int(chat)},
                                                "from": {"id": 7, "username": user}}}


def callback(update_id, data, chat="42", cb_id="cb1"):
    return {"update_id": update_id, "callback_query": {"id": cb_id, "data": data, "from": {"id": 7, "first_name": "Roy"},
                                                       "message": {"message_id": 55, "chat": {"id": int(chat)}}}}


class StubBackend:
    def __init__(self):
        self.calls = []

    def status(self): self.calls.append("status"); return "<b>status</b>"
    def screenshot(self, *a): self.calls.append(("screenshot", a)); return (None, "no frame")
    def sessions(self, limit): self.calls.append(("sessions", limit)); return "sessions"
    def ack(self, i, actor): self.calls.append(("ack", i, actor)); return f"acked {i} by {actor}"
    def snooze(self, i, m, actor): self.calls.append(("snooze", i, m, actor)); return f"snoozed {i} {m}"
    def report(self): self.calls.append("report"); return "report"


# ---------------------------------------------------------------- router

def test_parse_and_route_commands_with_authorisation_and_rate_limit():
    assert parse_command("/ack@my_bot INC-1") == ("ack", ["INC-1"])
    assert parse_command("hello") is None
    clock = FakeClock()
    audit = []
    b = StubBackend()
    r = CommandRouter(b, {"42"}, on_audit=audit.append, rate_per_minute=3, clock=clock)
    assert r.handle_update(msg(1, "/status"))[0].text == "<b>status</b>"
    assert r.handle_update(msg(2, "/status", chat="999")) == [] and "unauthorised" in audit[-1]
    assert "Usage" in r.handle_update(msg(3, "/ack"))[0].text
    assert "Usage" in r.handle_update(msg(4, "/snooze INC-1 abc"))[0].text    # rate: 3 allowed per minute -> 4th dropped
    assert r.handle_update(msg(5, "/report")) == [] and "rate limit" in audit[-1]
    clock.advance(61)
    assert r.handle_update(msg(6, "/snooze INC-1 5000"))[0].text == "snoozed INC-1 1440"   # clamped to 24 h
    assert r.handle_update(msg(7, "/help"))[0].text.startswith("<b>Commands</b>")
    assert "Unknown command" in r.handle_update(msg(8, "/reboot now"))[0].text        # no arbitrary operations
    assert r.handle_update({"update_id": 9, "message": {"chat": {"id": 42}, "photo": []}}) == []


def test_callback_buttons_are_authorised_and_answered():
    b = StubBackend()
    r = CommandRouter(b, {"42"})
    rep = r.handle_update(callback(1, "ack:INC-7"))[0]
    assert rep.callback_id == "cb1" and rep.text == "acked INC-7 by telegram:Roy" and rep.reply_to == 55
    rep = r.handle_update(callback(2, "snooze:INC-7:30"))[0]
    assert b.calls[-1] == ("snooze", "INC-7", 30, "telegram:Roy")
    rep = r.handle_update(callback(3, "ack:INC-7", chat="1"))[0]
    assert rep.callback_text == "Not authorised" and rep.text == ""
    assert r.handle_update(callback(4, "rm -rf"))[0].callback_text == "Unknown button"
    kb = incident_keyboard("INC-9")
    assert kb["inline_keyboard"][0][0]["callback_data"] == "ack:INC-9" and len(kb["inline_keyboard"]) == 2


# ---------------------------------------------------------------- poller

def make_client(transport, timeout=35.0):
    return TelegramClient(TelegramConfig(timeout_seconds=timeout), TOKEN_A, "42", None, transport=transport)


def test_poller_persists_offset_lease_and_handles_conflict():
    clock = FakeClock()
    tr = TgTransport()
    state = {}
    b = StubBackend()
    router = CommandRouter(b, {"42"}, clock=clock)
    p = UpdatePoller(make_client(tr), router, lambda k, d=None: state.get(k, d), state.__setitem__, "botA", "me", clock=clock)
    tr.updates.append([msg(10, "/status"), msg(11, "/report"), callback(12, "shot")])
    assert p.poll_once(timeout=0) == 3
    assert state["tg_updates_offset:botA"] == 13 and b.calls[:2] == ["status", "report"]
    assert [m for m, _ in tr.sent] == ["sendMessage", "sendMessage", "answerCallbackQuery", "sendMessage"]
    assert tr.sent[0][1]["chat_id"] == "42" and tr.sent[0][1]["reply_to_message_id"] == "100"
    # replayed update ids below the offset are ignored (at-least-once delivery from Telegram)
    tr.updates.append([msg(11, "/report"), msg(13, "/sessions")])
    assert p.poll_once(timeout=0) == 1 and b.calls[-1] == ("sessions", 10)
    # another in-process consumer holds the lease -> this poller stands down
    state["tg_consumer_lease:botA"] = {"owner": "other", "expires": clock.now + 60}
    tr.updates.append([msg(14, "/status")])
    assert p.poll_once(timeout=0) == 0 and tr.updates          # not consumed
    clock.advance(61)
    state.pop("tg_consumer_lease:botA")
    # Telegram 409: another consumer elsewhere -> back off 60 s and report once
    events = []
    p.on_event = events.append
    tr.fail_get_updates = DeliveryError("HTTP 409 Conflict: terminated by other getUpdates request", permanent=True)
    assert p.poll_once(timeout=0) == 0 and p.conflicts == 1 and "another consumer" in events[-1]
    assert p.poll_once(timeout=0) == 0                      # still backing off
    clock.advance(61)
    assert p.poll_once(timeout=0) == 1


# ---------------------------------------------------------------- monitor backend (standalone)

def harness_with_commands(cfg, rules, clock, tr):
    h = Harness(cfg, rules, clock)
    factory = ClientFactory(cfg.telegram, h.registry.token_for, transport=tr)
    bot = cfg.bots[0]
    client = TelegramClient(cfg.telegram, TOKEN_A, bot.chat_id, None, transport=tr)
    router = CommandRouter(h.mon.command_backend(), {bot.chat_id}, on_audit=h.events.append, clock=clock)
    h.mon.command_poller = UpdatePoller(client, router, h.queue.get_state, h.queue.set_state, bot.bot_id, "dev:1",
                                        on_event=h.events.append, clock=clock)
    return h, factory


def test_incident_alert_carries_buttons_and_commands_ack_snooze_status(cfg, rules, clock):
    tr = TgTransport()
    h, _ = harness_with_commands(cfg, rules, clock, tr)
    h.ocr.default = RESTRICTION
    h.run(6)
    alerts = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"]
    assert alerts and alerts[0]["payload"]["buttons"]["inline_keyboard"][0][0]["callback_data"].startswith("ack:INC-")
    inc_id = alerts[0]["event_id"]
    # /status answers with Studio/broadcast/incident lines
    tr.updates.append([msg(1, "/status")])
    h.run(2)
    status = tr.texts()[-1]
    assert "status</b>" in status and inc_id in status and "Open incidents: 1" in status
    # ack via button pauses reminders but keeps the incident open
    tr.updates.append([callback(2, f"ack:{inc_id}")])
    h.run(2)
    inc = h.mon.incident_engine.get(inc_id)
    assert inc.acknowledged and inc.is_open and inc.acknowledged_by == "telegram:Roy"
    assert any("Acknowledged" in t for t in tr.texts())
    # snooze and unknown incident
    tr.updates.append([msg(3, f"/snooze {inc_id} 15"), msg(4, "/ack INC-NOPE")])
    h.run(2)
    assert any("Snoozed" in t for t in tr.texts()) and any("Unknown incident" in t for t in tr.texts())
    assert h.mon.incident_engine.is_snoozed(h.mon.device_id, incident_id=inc_id)
    # /report and /sessions
    tr.updates.append([msg(5, "/report"), msg(6, "/sessions")])
    h.run(2)
    assert any("session report" in t for t in tr.texts()) and any("Recent sessions" in t for t in tr.texts())


def test_screenshot_command_sends_redacted_frame_or_explains(cfg, rules, clock):
    tr = TgTransport()
    h, _ = harness_with_commands(cfg, rules, clock, tr)
    h.run(6)                                                  # fresh frame cached
    tr.updates.append([msg(1, "/screenshot")])
    h.run(2)
    photos = [f for m, f in tr.sent if m == "sendPhoto"]
    assert len(photos) == 1 and photos[0]["_size"] > 100
    h.close_studio()
    h.run(30)
    clock.advance(600)
    tr.updates.append([msg(2, "/screenshot")])
    h.run(2)
    assert "No screenshot available" in tr.texts()[-1]


def test_buttons_can_be_disabled(cfg, rules, clock):
    cfg.commands.buttons = False
    h = Harness(cfg, rules, clock)
    h.ocr.default = RESTRICTION
    h.run(6)
    alerts = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"]
    assert alerts and "buttons" not in alerts[0]["payload"]


# ---------------------------------------------------------------- escalation route + e-mail backup

def test_escalation_route_after_unacknowledged_reminders(cfg, rules, clock):
    cfg.escalation.enabled, cfg.escalation.after_reminders, cfg.escalation.chat_id = True, 2, "-100777"
    h = Harness(cfg, rules, clock)
    h.ocr.default = RESTRICTION
    h.run(6)
    h.run(330)                                                # first reminder (5 min) -> no escalation route yet
    routed = [d for d in all_deliveries(h.queue) if "-X" in d["event_id"]]
    assert routed == []
    h.run(320)                                                # second reminder -> escalation route
    routed = [d for d in all_deliveries(h.queue) if "-X" in d["event_id"]]
    assert len(routed) == 1 and routed[0]["chat_id"] == "-100777" and "ESCALATION" in routed[0]["payload"]["text"]


def test_email_backup_on_failed_urgent_delivery(cfg, rules, clock):
    sent = []
    settings = SmtpSettings(True, "smtp.example.org", 587, "", "monitor@example.org", ["roy@example.org"])
    backup = EmailBackup(settings, None, sender=lambda s, pw, m: sent.append(m))
    cfg.telegram.max_attempts = 1
    h = Harness(cfg, rules, clock)
    h.mon.email_backup = backup
    h.ocr.default = RESTRICTION
    h.run(6)
    d = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"][0]
    h.queue.mark_failed(d["id"], "Forbidden: bot was blocked by the user", permanent=True)
    h.run(2)
    assert len(sent) == 1 and "Telegram failed" in sent[0]["Subject"] and "roy@example.org" == sent[0]["To"]
    assert "Community Guidelines" in sent[0].get_content()
    h.run(10)
    assert len(sent) == 1                                     # once per failed delivery
    # unconfigured / missing password -> no send, explicit reason
    eb2 = EmailBackup(SmtpSettings(True, "h", 587, "user", "a@b", ["c@d"]), __import__("studio_monitor.credentials", fromlist=["x"]).MemoryCredentialStore(),
                      sender=lambda *a: None)
    assert eb2.send("s", "b") is False and "password missing" in eb2.last_error
    assert EmailBackup(SmtpSettings(False), None).configured is False


# ---------------------------------------------------------------- hub side

def test_hub_commands_poll_ack_and_request_screenshot(hubfx_factory, cfg, rules, clock):
    hubfx, tr = hubfx_factory()
    from test_hub_agent import managed_harness, ADMIN
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    h, sync = managed_harness(hubfx, cfg, rules, clock)
    hubfx.tc.post("/api/v1/routes", json={"name": "ops", "token_env": "HUB_BOT_OPS", "chat_id": "42", "commands_enabled": True}, headers=ADMIN)
    hubfx.app.state.worker.env = {"HUB_BOT_OPS": TOKEN_A}
    h.ocr.default = RESTRICTION
    h.run(8)
    incs = hubfx.incidents()
    assert len(incs) == 1
    out = hubfx.app.state.background.once()
    assert out["delivered"] >= 1                               # hub routed the restriction alert (managed)
    tr.updates.append([msg(1, "/status"), callback(2, f"ack:{incs[0]['incident_id']}"), msg(3, "/screenshot")])
    out = hubfx.app.state.background.once()
    assert out["commands"] == 3
    texts = tr.texts()
    assert any("Fleet status" in t for t in texts) and any("Acknowledged" in t for t in texts)
    assert hubfx.incidents()[0]["acked_by"] == "telegram:Roy"
    # the hub answers with the latest stored evidence as a photo (caption carries the note) or with text
    assert any("Screenshot requested" in t for t in texts) or any(m == "sendPhoto" for m, _ in tr.sent)
    # the agent picks the predefined screenshot op up with its next heartbeat and mirrors a SCREENSHOT event with evidence
    clock.advance(16)
    h.run(6)
    types = [t for t, _s, _e in hubfx.events(cfg.device.device_id)]
    assert "SCREENSHOT" in types
    assert any(e for t, _s, e in hubfx.events(cfg.device.device_id) if t == "SCREENSHOT" and e)
    assert any("remote screenshot captured" in e for e in h.events)
    # arbitrary ops are ignored
    h.mon.execute_remote_command({"op": "shell", "cmd": "format c:"})
    assert any("not a predefined operation" in e for e in h.events)


@pytest.fixture
def hubfx_factory(tmp_path, clock):
    from test_hub_agent import HubFixture
    created = []

    def make():
        tr = TgTransport()
        fx = HubFixture(tmp_path, clock)
        # swap the hub's Telegram transport for the scripted one (delivery worker + command pollers share it)
        fx.app.state.worker.transport = tr
        fx.app.state.background.commands.transport = tr
        created.append(fx)
        return fx, tr
    yield make
    for fx in created:
        fx.tc.__exit__(None, None, None)
