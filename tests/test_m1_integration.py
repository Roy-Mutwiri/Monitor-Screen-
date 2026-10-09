"""Milestone 1 integration: incident engine wired into the monitor, threaded
follow-ups, escalation reminders, maintenance suppression, schedules and
managed mode. Fully mocked (no Telegram)."""
from datetime import datetime, timezone

import pytest

from conftest import TOKEN_A, FakeTransport, all_deliveries, make_window
from test_activity import LIVE_TEXT, NOT_LIVE_TEXT, Harness

from studio_monitor.config import AppConfig
from studio_monitor.contracts.events import Severity
from studio_monitor.incident_engine import OPEN, RESOLVED
from studio_monitor.queue import DeliveryWorker
from studio_monitor.telegram import ClientFactory

POPUP = "Your LIVE has been restricted for violating our Community Guidelines"
PUZZLE = "Verify to continue: drag the slider to fit the puzzle piece"


def test_device_id_generated_and_persisted(tmp_path):
    cfg = AppConfig()
    assert cfg.device.device_id == ""
    p = tmp_path / "c.json"
    cfg.save(p)
    assert len(cfg.device.device_id) == 36 and AppConfig.load(p).device.device_id == cfg.device.device_id
    assert AppConfig.load(p).device.device_name == cfg.machine_label


def test_restriction_creates_durable_incident_and_resolves_with_honest_wording(cfg, rules, clock):
    cfg.detection.resolve_after_seconds = 20
    h = Harness(cfg, rules, clock)
    h.ocr.default = POPUP
    h.run(4)
    incs = h.mon.incident_engine.list(status=OPEN)
    assert len(incs) == 1 and incs[0].severity == Severity.URGENT and incs[0].category == "restrictions"
    assert incs[0].incident_id == [d for d in all_deliveries(h.queue) if d["kind"] == "incident"][0]["event_id"]
    h.ocr.default = NOT_LIVE_TEXT                       # popup gone
    h.run(40)
    inc = h.mon.incident_engine.get(incs[0].incident_id)
    assert inc.status == RESOLVED and "no longer visible" in inc.resolution and "does not confirm" in inc.resolution
    res = [d for d in all_deliveries(h.queue) if d["event_id"].endswith("-RES")]
    assert len(res) == 1 and res[0]["payload"]["thread_of"] == inc.incident_id and "RESOLVED" in res[0]["payload"]["text"]


def test_follow_ups_reply_to_root_message_per_destination(cfg, rules, clock):
    cfg.activity.notify_already_running = False
    h = Harness(cfg, rules, clock)
    t = FakeTransport([(200, {"ok": True, "result": {"message_id": 4242}}), (200, {"ok": True, "result": {"message_id": 4300}})])
    h.mon.client_factory = ClientFactory(cfg.telegram, h.registry.token_for, transport=t)
    h.ocr.default = PUZZLE
    h.run(2)
    inc_id = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"][0]["event_id"]
    DeliveryWorker(h.queue, h.mon.send_delivery).process_round()           # first alert -> root message 4242
    assert h.mon.incident_engine.root_message(inc_id, h.registry.bots[0].bot_id, "42") == 4242
    clock.advance(301); h.run(2)                                             # URGENT escalation after 5 min
    esc = [d for d in all_deliveries(h.queue) if "-E1" in d["event_id"]]
    assert len(esc) == 1 and esc[0]["payload"]["thread_of"] == inc_id
    DeliveryWorker(h.queue, h.mon.send_delivery).process_round()
    body = t.requests[-1][1]
    assert b"reply_to_message_id" in body and b"4242" in body                 # threaded under the root


def test_acknowledged_incident_does_not_escalate(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.ocr.default = PUZZLE
    h.run(2)
    inc_id = h.mon.incident_engine.list(status=OPEN)[0].incident_id
    h.mon.incident_engine.acknowledge(inc_id, "tg:1", "on it")
    clock.advance(1000); h.run(4)
    assert not any("-E1" in d["event_id"] for d in all_deliveries(h.queue))
    assert h.mon.incident_engine.get(inc_id).status == OPEN               # ack is not resolution


def test_maintenance_withholds_selected_categories_but_not_critical(cfg, rules, clock):
    h = Harness(cfg, rules, clock)
    h.mon.incident_engine.enter_maintenance(cfg.device.device_id, 3600, ["studio_opened", "reminders", "restrictions"])
    h.run(4)                                                                 # "already running" -> studio_opened suppressed
    opened = [d for d in all_deliveries(h.queue) if "ALREADY RUNNING" in d["payload"]["caption"]]
    assert opened == [] and any("STUDIO_ALREADY_RUNNING" == e["event_type"] for e in h.queue.recent_events())
    h.ocr.default = POPUP
    h.run(2)
    assert len([d for d in all_deliveries(h.queue) if d["kind"] == "incident"]) == 1   # critical still delivered


def test_managed_mode_records_events_without_local_deliveries(cfg, rules, clock):
    cfg.device.mode = "managed"
    h = Harness(cfg, rules, clock)
    h.ocr.default = POPUP
    h.run(4)
    assert all_deliveries(h.queue) == [] and len(h.queue.events_history(10, "incident")) == 1


def test_schedule_gates_reminders_and_reports_missed_start(cfg, rules, clock):
    # clock starts 1,000,000 = 1970-01-12 13:46:40 UTC (Monday). Window 13:00-15:00 UTC, grace 10 min.
    cfg.device.schedule = {"enabled": True, "timezone": "UTC", "weekdays": ["mon"], "start": "13:00", "end": "15:00", "grace_minutes": 10}
    cfg.activity.offline_threshold_minutes = 5
    h = Harness(cfg, rules, clock)
    h.run(6 * 60, step=10)                                                   # confirmed NOT_LIVE inside the window
    missed = [d for d in all_deliveries(h.queue) if "SCHEDULED START MISSED" in d["payload"]["text"]]
    assert len(missed) == 1
    assert h.mon.reminders.enabled                                           # reminders allowed inside the window
    h.run(10 * 60, step=10)
    assert len([d for d in all_deliveries(h.queue) if "SCHEDULED START MISSED" in d["payload"]["text"]]) == 1
    assert len([d for d in all_deliveries(h.queue) if d["kind"] == "reminder"]) == 1
    clock.advance(2 * 3600)                                                  # 16:xx UTC: outside the window
    h.run(60, step=10)
    assert not h.mon.reminders.enabled


def test_unknown_never_counts_as_missed_start(cfg, rules, clock):
    cfg.device.schedule = {"enabled": True, "timezone": "UTC", "weekdays": ["mon"], "start": "13:00", "end": "15:00", "grace_minutes": 1}
    h = Harness(cfg, rules, clock)
    h.ocr.default = "Loading... please wait"
    h.run(10 * 60, step=10)
    assert not any("SCHEDULED START MISSED" in d["payload"]["text"] for d in all_deliveries(h.queue))
