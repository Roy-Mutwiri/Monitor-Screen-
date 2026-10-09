"""Bot registry, secure storage, subscriptions, multi-bot delivery through
the monitor, migration, test notifications."""
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from conftest import (TOKEN_A, TOKEN_B, TOKEN_C, FakeCapturer, FakeClock, FakeOcr, FakeTransport, FakeWindowSystem,
                      all_deliveries, make_token, make_window)

from studio_monitor import app as appmod
from studio_monitor.bot_tests import deliver_test_now, enqueue_test, validate_bot, validate_token
from studio_monitor.bots import (CAT_HEALTH, CAT_REMINDERS, CAT_RESTRICTIONS, CAT_STUDIO_CLOSED, CAT_STUDIO_OPENED,
                                 CAT_VERIFICATION, EVENT_CATEGORIES, MAX_BOTS, BotError, BotRegistry, BotTarget)
from studio_monitor.config import AppConfig
from studio_monitor.credentials import CredentialError, MemoryCredentialStore
from studio_monitor.monitor import Monitor
from studio_monitor.queue import DeliveryQueue, DeliveryWorker
from studio_monitor.regions import Region
from studio_monitor.telegram import ClientFactory, TokenRedactingFilter, sanitize

POPUP = "Your LIVE has been restricted for violating our Community Guidelines"
PUZZLE = "Verify to continue: drag the slider to fit the puzzle piece"


def make_registry(cfg, tmp_path, store=None, queue=None):
    store = store or MemoryCredentialStore()
    path = tmp_path / "config.json"
    return BotRegistry(cfg, store, save=lambda: cfg.save(path), queue=queue), store, path


# ---------------------------------------------------------------- registry

def test_add_stores_token_securely_and_settings_hold_only_reference(cfg, tmp_path):
    reg, store, path = make_registry(cfg, tmp_path)
    bot = reg.add("Alerts", TOKEN_A, "-1001234567890", thread_id="12")
    assert store.tokens[bot.bot_id] == TOKEN_A
    text = path.read_text(encoding="utf-8")
    assert TOKEN_A not in text and bot.bot_id in text and "memory:MonitorScreen/telegram-bot/" in text
    assert bot.chat_id == "-1001234567890" and bot.thread_id == 12 and bot.enabled
    assert bot.subscriptions == list(EVENT_CATEGORIES) and bot.token_fingerprint and bot.token_fingerprint != TOKEN_A
    back = AppConfig.load(path)
    assert back.bots[0].bot_id == bot.bot_id and back.telegram.bot_token == ""


def test_max_ten_bots_including_disabled(cfg, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    for i in range(MAX_BOTS):
        reg.add(f"bot{i}", make_token(i), str(i), enabled=(i % 2 == 0))
    assert reg.count == 10 and not reg.can_add
    with pytest.raises(BotError, match="Maximum of 10"):
        reg.add("eleventh", make_token(99), "99")


def test_duplicate_token_rejected_via_fingerprint(cfg, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "1")
    with pytest.raises(BotError, match="already used by bot 'A'"):
        reg.add("B", TOKEN_A, "2")
    b = reg.add("B", TOKEN_B, "2")
    with pytest.raises(BotError, match="already used"):
        reg.update(b.bot_id, new_token=TOKEN_A, new_token_identity=(1, "x"))


@pytest.mark.parametrize("bad", ["", "notatoken", "12:short", "my password"])
def test_token_format_validation(cfg, tmp_path, bad):
    reg, *_ = make_registry(cfg, tmp_path)
    with pytest.raises(BotError, match="BotFather"):
        reg.add("A", bad, "1")


@pytest.mark.parametrize("chat,ok", [("123", True), ("-100123", True), ("@my_channel", True), ("", False),
                                     ("abc", False), ("@a", False)])
def test_chat_id_validation(cfg, tmp_path, chat, ok):
    reg, *_ = make_registry(cfg, tmp_path)
    if ok:
        assert reg.add("A", TOKEN_A, chat).chat_id == chat
    else:
        with pytest.raises(BotError):
            reg.add("A", TOKEN_A, chat)


def test_topic_and_subscription_validation(cfg, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    with pytest.raises(BotError, match="topic"):
        reg.add("A", TOKEN_A, "1", thread_id="-3")
    with pytest.raises(BotError, match="Unknown event category"):
        reg.add("A", TOKEN_A, "1", subscriptions=["restrictions", "nonexistent"])
    bot = reg.add("A", TOKEN_A, "1", subscriptions=[CAT_RESTRICTIONS, CAT_RESTRICTIONS])
    assert bot.subscriptions == [CAT_RESTRICTIONS]


def test_edit_keeps_token_when_blank_and_updates_destination_for_future(cfg, tmp_path):
    reg, store, _ = make_registry(cfg, tmp_path)
    bot = reg.add("A", TOKEN_A, "1")
    reg.update(bot.bot_id, name="Renamed", chat_id="2", thread_id="9", new_token="")
    assert store.tokens[bot.bot_id] == TOKEN_A and bot.name == "Renamed" and bot.chat_id == "2" and bot.thread_id == 9
    reg.update(bot.bot_id, thread_id="")
    assert bot.thread_id is None


def test_token_rotation_checks_bot_identity(cfg, tmp_path):
    reg, store, _ = make_registry(cfg, tmp_path)
    bot = reg.add("A", TOKEN_A, "1", verified=(111, "a_bot"))
    with pytest.raises(BotError, match="different bot"):
        reg.update(bot.bot_id, new_token=TOKEN_B, new_token_identity=(222, "other_bot"))
    with pytest.raises(BotError, match="must be validated"):
        reg.update(bot.bot_id, new_token=TOKEN_B)
    reg.update(bot.bot_id, new_token=TOKEN_B, new_token_identity=(111, "a_bot"))
    assert store.tokens[bot.bot_id] == TOKEN_B and bot.verified_username == "a_bot"


def test_remove_deletes_credential_cancels_pending_keeps_history(cfg, tmp_path, clock):
    q = DeliveryQueue(cfg.db_path, clock=clock)
    reg, store, _ = make_registry(cfg, tmp_path, queue=q)
    bot = reg.add("A", TOKEN_A, "1")
    q.create_event("E1", "incident", "restrictions", {}, "", reg.targets(CAT_RESTRICTIONS))
    reg.remove(bot.bot_id)
    assert bot.bot_id not in store.tokens and reg.count == 0
    d = q.deliveries_for("E1")[0]
    assert d.status == "cancelled" and d.last_error == "bot removed" and d.bot_name == "A"


def test_disable_cancels_pending_and_excludes_from_new(cfg, tmp_path, clock):
    q = DeliveryQueue(cfg.db_path, clock=clock)
    reg, *_ = make_registry(cfg, tmp_path, queue=q)
    a = reg.add("A", TOKEN_A, "1")
    reg.add("B", TOKEN_B, "2")
    q.create_event("E1", "incident", "restrictions", {}, "", reg.targets(CAT_RESTRICTIONS))
    reg.set_enabled(a.bot_id, False)
    st = {d.bot_id: d.status for d in q.deliveries_for("E1")}
    assert st[a.bot_id] == "cancelled" and len(reg.targets(CAT_RESTRICTIONS)) == 1
    assert q.deliveries_for("E1")[0].last_error == "bot disabled"


def test_partial_write_failures_are_reported_and_rolled_back(cfg, tmp_path):
    store = MemoryCredentialStore()
    reg = BotRegistry(cfg, store, save=lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(BotError, match="not added"):
        reg.add("A", TOKEN_A, "1")
    assert reg.count == 0 and store.tokens == {}
    store.fail_set = True
    reg2, *_ = make_registry(cfg, tmp_path, store=store)
    with pytest.raises(CredentialError):
        reg2.add("A", TOKEN_A, "1")
    assert reg2.count == 0


def test_subscription_filtering_targets(cfg, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("All", TOKEN_A, "1")
    reg.add("OnlyPuzzles", TOKEN_B, "2", subscriptions=[CAT_VERIFICATION])
    reg.add("Off", TOKEN_C, "3", enabled=False)
    assert [t.bot_name for t in reg.targets(CAT_RESTRICTIONS)] == ["All"]
    assert sorted(t.bot_name for t in reg.targets(CAT_VERIFICATION)) == ["All", "OnlyPuzzles"]
    assert [t.bot_name for t in reg.targets("test")] == ["All", "OnlyPuzzles"]   # explicit tests ignore subscriptions


def test_config_never_enables_unknown_event_types(cfg, tmp_path):
    from studio_monitor.bots import BotConfig
    b = BotConfig.from_dict({"bot_id": "x", "name": "n", "chat_id": "1", "subscriptions": ["restrictions", "ghost"],
                             "thread_id": "5"})
    assert b.subscriptions == ["restrictions"] and b.thread_id == 5


# ---------------------------------------------------------------- monitor fan-out

ALERTS = [CAT_RESTRICTIONS, CAT_VERIFICATION, CAT_REMINDERS]   # no health/activity noise in fan-out tests


def incidents(queue):
    return [d for d in all_deliveries(queue) if d["kind"] == "incident"]


def build(cfg, rules, clock, reg, transport=None):
    cfg.activity.notify_already_running = False
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    cap = FakeCapturer()
    ocr = FakeOcr()
    queue = DeliveryQueue(cfg.db_path, clock=clock, sanitizer=sanitize)
    reg.queue = queue
    factory = ClientFactory(cfg.telegram, reg.token_for, transport=transport) if transport else None
    events = []
    from studio_monitor.broadcast import LiveRules
    live_rules = LiveRules.load(Path(__file__).resolve().parents[1] / "rules" / "live_state_rules.json")
    mon = Monitor(cfg, sys_, cap, ocr, rules, queue, reg, factory, clock, on_event=events.append, mono=clock,
                  live_rules=live_rules)
    return mon, sys_, cap, ocr, queue, events


def test_delivery_to_multiple_enabled_bots_with_own_destinations(cfg, rules, clock, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "11", subscriptions=ALERTS)
    reg.add("B", TOKEN_B, "-100222", thread_id="7", subscriptions=ALERTS)
    reg.add("Off", TOKEN_C, "33", enabled=False)
    t = FakeTransport()
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg, transport=t)
    ocr.default = POPUP
    mon.tick()
    ds = incidents(queue)
    assert sorted(d["bot_name"] for d in ds) == ["A", "B"] and len(all_deliveries(queue)) == 2
    assert len({d["screenshot_path"] for d in ds}) == 1 and Path(ds[0]["screenshot_path"]).exists()
    DeliveryWorker(queue, mon.send_delivery).process_round()
    assert {d["status"] for d in all_deliveries(queue)} == {"sent"}
    a = t.sent_to(TOKEN_A)[0]; b = t.sent_to(TOKEN_B)[0]
    assert a[0].endswith("/sendPhoto") and b'name="chat_id"\r\n\r\n11\r\n' in a[1]
    assert b'name="chat_id"\r\n\r\n-100222\r\n' in b[1] and b'name="message_thread_id"\r\n\r\n7\r\n' in b[1]
    assert t.sent_to(TOKEN_C) == []


def test_subscriptions_route_categories(cfg, rules, clock, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("Puzzles", TOKEN_A, "1", subscriptions=[CAT_VERIFICATION])
    reg.add("Restrictions", TOKEN_B, "2", subscriptions=[CAT_RESTRICTIONS])
    cfg.activity.notify_already_running = False
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    ocr.default = PUZZLE
    mon.tick()
    assert [d["bot_name"] for d in all_deliveries(queue)] == ["Puzzles"]
    clock.advance(700)
    ocr.default = POPUP
    mon.tick()
    alerts = [d for d in all_deliveries(queue) if d["event_id"].count("-") == 3]   # exclude -RES / -E1 follow-ups
    assert sorted(d["bot_name"] for d in alerts) == ["Puzzles", "Restrictions"]


def test_one_failing_bot_and_independent_rate_limits_in_monitor(cfg, rules, clock, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("Good", TOKEN_A, "1", subscriptions=ALERTS)
    reg.add("Bad", TOKEN_B, "2", subscriptions=ALERTS)
    t = FakeTransport(per_token={TOKEN_B: [(429, {"ok": False, "description": "slow down", "parameters": {"retry_after": 40}})]})
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg, transport=t)
    ocr.default = POPUP
    mon.tick()
    w = DeliveryWorker(queue, mon.send_delivery)
    w.process_round()
    st = {d["bot_name"]: d["status"] for d in all_deliveries(queue)}
    assert st == {"Good": "sent", "Bad": "pending"}
    assert w.process_round() == 0                     # Bad is rate-limited; nothing else due
    clock.advance(41)
    w.process_round()
    assert {d["status"] for d in all_deliveries(queue)} == {"sent"}


def test_health_alerts_only_to_subscribed_bots(cfg, rules, clock, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("Health", TOKEN_A, "1", subscriptions=[CAT_HEALTH])
    reg.add("Quiet", TOKEN_B, "2", subscriptions=[CAT_RESTRICTIONS])
    cfg.activity.notify_already_running = False
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    mon.tick()
    sys_.remove(0x1001)                                   # window gone, process alive -> degraded for > 15 s
    for _ in range(10):
        clock.advance(2); mon.tick()
    ds = [d for d in all_deliveries(queue) if d["kind"] == "status"]
    assert ds and {d["bot_name"] for d in ds} == {"Health"}


def test_destination_edit_does_not_redirect_queued_evidence(cfg, rules, clock, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    bot = reg.add("A", TOKEN_A, "11", subscriptions=ALERTS)
    t = FakeTransport()
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg, transport=t)
    ocr.default = POPUP
    mon.tick()
    reg.update(bot.bot_id, chat_id="99")
    DeliveryWorker(queue, mon.send_delivery).process_round()
    assert b'name="chat_id"\r\n\r\n11\r\n' in t.sent_to(TOKEN_A)[0][1]
    clock.advance(700)
    mon.tick()                                        # new event -> new destination
    DeliveryWorker(queue, mon.send_delivery).process_round()
    assert b'name="chat_id"\r\n\r\n99\r\n' in t.sent_to(TOKEN_A)[1][1]


def test_restart_recovery_no_duplicate_delivery_rows(cfg, rules, clock, tmp_path):
    reg, store, _ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "1", subscriptions=ALERTS)
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    ocr.default = POPUP
    mon.tick()
    before = all_deliveries(queue)
    queue.close()
    # "restart": new queue/monitor over the same database; the pending delivery must survive
    # untouched (no duplicate row) and still be deliverable
    mon2, sys2, cap2, ocr2, queue2, events2 = build(cfg, rules, clock, reg)
    for _ in range(3):
        mon2.tick(); clock.advance(2)
    after = all_deliveries(queue2)
    assert len(after) == len(before) == 1
    assert after[0]["event_id"] == before[0]["event_id"] and after[0]["status"] == "pending"
    assert queue2.create_event(before[0]["event_id"], "incident", "restrictions", {}, "", reg.targets(CAT_RESTRICTIONS)) == 0
    assert [d.event_id for d in queue2.due_deliveries()] == [before[0]["event_id"]]


def test_reminder_fans_out_and_cancels_for_all_bots(cfg, rules, clock, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "1")
    reg.add("B", TOKEN_B, "2", subscriptions=[CAT_REMINDERS])
    reg.add("C", TOKEN_C, "3", subscriptions=[CAT_RESTRICTIONS])
    cfg.activity.notify_already_running = False
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    ocr.default = "Go LIVE  Preview  Add a title"
    for _ in range(int(61 * 60 / 10)):
        mon.tick(); clock.advance(10)
    rem = [d for d in all_deliveries(queue) if d["kind"] == "reminder"]
    assert sorted(d["bot_name"] for d in rem) == ["A", "B"]
    ocr.default = "LIVE 00:12:34  1,204 viewers  End LIVE"
    for _ in range(6):
        mon.tick(); clock.advance(10)
    assert all(d["status"] == "cancelled" for d in all_deliveries(queue) if d["kind"] == "reminder")


def test_shared_evidence_retained_until_eligible_deliveries_finish(cfg, rules, clock, tmp_path):
    cfg.privacy.screenshot_retention_days = 1
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "1")
    reg.add("B", TOKEN_B, "2")
    cfg.activity.notify_already_running = False
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    ocr.default = POPUP
    mon.tick()
    shot = Path(all_deliveries(queue)[0]["screenshot_path"])
    import os
    old = clock() - 3 * 86400
    os.utime(shot, (old, old))
    ds = queue.deliveries_for(all_deliveries(queue)[0]["event_id"])
    queue.mark_sent(ds[0].id, 1)                       # A delivered, B still pending
    clock.advance(3601); mon._last_purge = 0
    ocr.default = ""
    mon.tick()
    assert shot.exists()                               # retained: B still needs it
    queue.mark_failed(ds[1].id, "blocked", permanent=True)
    clock.advance(3601)
    mon.tick()
    assert not shot.exists()                           # all deliveries terminal -> normal retention applies


def test_dead_letter_bounds_retention(cfg, rules, clock, tmp_path):
    cfg.privacy.screenshot_retention_days = 1
    cfg.telegram.delivery_max_age_hours = 2
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "1")
    cfg.activity.notify_already_running = False
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    ocr.default = POPUP
    mon.tick()
    shot = Path(all_deliveries(queue)[0]["screenshot_path"])
    import os
    old = clock() - 3 * 86400
    os.utime(shot, (old, old))
    clock.advance(3 * 3600); mon._last_purge = 0
    ocr.default = ""
    mon.tick()
    d = all_deliveries(queue)[0]
    assert d["status"] == "dead" and "expired" in d["last_error"] and not shot.exists()


# ---------------------------------------------------------------- migration

def test_single_bot_migration_with_pending_and_delivered_events(cfg, tmp_path, clock):
    path = tmp_path / "config.json"
    cfg.telegram.bot_token = TOKEN_A
    cfg.telegram.chat_id = "4242"
    cfg.telegram.notify_status_changes = False
    # legacy outbox rows
    conn = sqlite3.connect(cfg.db_path.parent.mkdir(parents=True, exist_ok=True) or cfg.db_path)
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, payload TEXT NOT NULL, "
                 "screenshot_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
                 "next_attempt_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, sent_at REAL, last_error TEXT NOT NULL DEFAULT '', "
                 "kind TEXT NOT NULL DEFAULT 'incident')")
    conn.execute("INSERT INTO alerts (incident_id, payload, screenshot_path, status, created_at) VALUES ('INC-1', '{\"text\":\"p\"}', '/s/1.png', 'pending', 1)")
    conn.execute("INSERT INTO alerts (incident_id, payload, status, created_at, sent_at) VALUES ('INC-2', '{\"text\":\"s\"}', 'sent', 2, 3)")
    conn.commit(); conn.close()
    store = MemoryCredentialStore()
    queue = DeliveryQueue(cfg.db_path, clock=clock)
    reg = appmod.make_registry(cfg, path, queue, store)
    notes = appmod.run_migrations(cfg, path, reg, queue)
    assert any("Default Bot" in n for n in notes) and any("legacy outbox" in n for n in notes)
    bot = reg.by_name("Default Bot")
    assert bot and store.tokens[bot.bot_id] == TOKEN_A and bot.chat_id == "4242" and CAT_HEALTH not in bot.subscriptions
    assert cfg.telegram.bot_token == "" and TOKEN_A not in path.read_text(encoding="utf-8")
    ds = {d.event_id: d for d in all_deliveries_objs(queue)}
    assert ds["INC-1"].status == "pending" and ds["INC-2"].status == "sent"
    # restart-safe: running again changes nothing
    cfg.telegram.bot_token = TOKEN_A; cfg.telegram.chat_id = "4242"
    appmod.run_migrations(cfg, path, reg, queue)
    assert reg.count == 1 and len(all_deliveries(queue)) == 2


def all_deliveries_objs(queue):
    rows = queue._conn.execute("SELECT event_id FROM deliveries").fetchall()
    return [queue.deliveries_for(r[0])[0] for r in rows]


# ---------------------------------------------------------------- validation + test notifications

def test_validate_uses_get_me_only_and_records_identity(cfg, tmp_path):
    reg, *_ = make_registry(cfg, tmp_path)
    bot = reg.add("A", TOKEN_A, "1")
    t = FakeTransport([(200, {"ok": True, "result": {"id": 777, "username": "alerts_bot", "first_name": "Alerts"}})])
    info = validate_bot(ClientFactory(cfg.telegram, reg.token_for, transport=t), reg, bot.bot_id)
    assert info["username"] == "alerts_bot" and bot.verified_bot_id == 777 and bot.verified_username == "alerts_bot"
    assert len(t.requests) == 1 and t.requests[0][0].endswith("/getMe")


def test_test_notification_is_synthetic_and_single_bot(cfg, tmp_path, clock):
    reg, *_ = make_registry(cfg, tmp_path)
    a = reg.add("A", TOKEN_A, "1")
    reg.add("B", TOKEN_B, "2")
    queue = DeliveryQueue(cfg.db_path, clock=clock)
    t = FakeTransport([(200, {"ok": True, "result": {"message_id": 12}})])
    factory = ClientFactory(cfg.telegram, reg.token_for, transport=t)
    eid = enqueue_test(queue, reg, cfg, a.bot_id, clock)
    ds = all_deliveries(queue)
    assert len(ds) == 1 and ds[0]["bot_name"] == "A" and ds[0]["kind"] == "test"
    img = Image.open(ds[0]["screenshot_path"])
    assert img.size == (900, 420) and img.getpixel((5, 5)) == (255, 193, 7)   # synthetic banner, not a capture
    assert "TEST NOTIFICATION" in ds[0]["payload"]["caption"] and "synthetic" in ds[0]["payload"]["caption"]
    res = deliver_test_now(queue, reg, factory, eid, clock)
    assert "delivered" in res and a.last_test_result.startswith("test delivered") and t.sent_to(TOKEN_B) == []


def test_token_redaction_in_logs_and_errors(cfg, tmp_path, caplog):
    logger = logging.getLogger("studio_monitor.test")
    logger.addFilter(TokenRedactingFilter())
    with caplog.at_level(logging.INFO, logger="studio_monitor.test"):
        logger.info("posting to https://api.telegram.org/bot%s/sendPhoto", TOKEN_A)
    assert TOKEN_A not in caplog.text and "[REDACTED]" in caplog.text
    reg, *_ = make_registry(cfg, tmp_path)
    bot = reg.add("A", TOKEN_A, "1")
    t = FakeTransport([(500, {"ok": False, "description": f"boom bot{TOKEN_A}"})])
    with pytest.raises(Exception) as e:
        validate_token(ClientFactory(cfg.telegram, reg.token_for, transport=t), TOKEN_A)
    assert TOKEN_A not in str(e.value)
    assert TOKEN_A not in json.dumps(cfg.to_dict())


def test_privacy_masks_apply_to_shared_evidence(cfg, rules, clock, tmp_path):
    cfg.regions = [Region("chat", 0.5, 0.0, 0.5, 1.0, "redact")]
    reg, *_ = make_registry(cfg, tmp_path)
    reg.add("A", TOKEN_A, "1")
    reg.add("B", TOKEN_B, "2")
    mon, sys_, cap, ocr, queue, events = build(cfg, rules, clock, reg)
    ocr.default = POPUP
    mon.tick()
    ds = incidents(queue)
    assert len(ds) == 2
    for d in ds:
        img = Image.open(d["screenshot_path"])
        assert img.getpixel((img.width - 1, 0)) == (0, 0, 0)
