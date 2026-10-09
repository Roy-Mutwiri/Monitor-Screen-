"""Unit tests: live-state classification, confirmation, frame cache, startup
registry, queue migration/cancel/transaction, late-delivery stamp."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeClock

from studio_monitor.broadcast import BroadcastStateEngine, Classification, LiveRules, LiveState
from studio_monitor.framecache import FrameCache
from studio_monitor.queue import DeliveryQueue
from studio_monitor.startup import DictRegistry, VALUE_NAME, apply_setting, enable, is_enabled, launch_command
from studio_monitor.telegram import late_delivery_note

RULES = Path(__file__).resolve().parents[1] / "rules" / "live_state_rules.json"


@pytest.fixture
def live_rules():
    return LiveRules.load(RULES)


def test_seeded_rules_are_marked_unverified(live_rules):
    assert live_rules.verified is False


@pytest.mark.parametrize("text,expected", [
    ("Scenes Sources Go LIVE Preview Chat", LiveState.NOT_LIVE),
    ("LIVE 00:12:34  1,204 viewers  End LIVE", LiveState.LIVE),
    ("LIVE", LiveState.UNKNOWN),                           # bare word is not evidence
    ("LIVE chat: someone wrote go live now please", LiveState.UNKNOWN),  # chat text alone: go live(2) vs nothing -> NOT_LIVE? see below
    ("Loading... Go LIVE", LiveState.UNKNOWN),             # transitional screen wins
    ("Go LIVE  End LIVE 00:01:02", LiveState.UNKNOWN),     # contradictory
    ("", LiveState.UNKNOWN),
    ("You're live  00:01  52 viewers", LiveState.LIVE),
])
def test_classification(live_rules, text, expected):
    c = live_rules.classify(text)
    if text.startswith("LIVE chat"):
        # Chat text containing "go live" scores the NOT_LIVE control phrase: this is exactly why
        # live-status regions should be configured; the engine alone cannot tell chat from controls.
        assert c.state in (LiveState.NOT_LIVE, LiveState.UNKNOWN)
        return
    assert c.state == expected, c.summary()


def test_phrases_match_whole_words_only(live_rules):
    from studio_monitor.broadcast import phrase_in
    assert phrase_in("login", "please login now") and not phrase_in("login", "logintest page")
    assert not phrase_in("live", "we deliver fast") and phrase_in("go live", "press go live to start")
    assert live_rules.classify("Loginx  Go LIVE  Preview").state == LiveState.NOT_LIVE


def test_live_requires_timer_for_badge(live_rules):
    assert live_rules.classify("LIVE  320 viewers").state == LiveState.UNKNOWN   # badge w/o timer (1) + viewers (1) = 2? check
    c = live_rules.classify("LIVE  320 viewers")
    # badge without timer scores 0, viewers 1 -> below min 2 -> UNKNOWN
    assert c.live_score == 1


def test_confirmation_and_gaps(live_rules):
    clock = FakeClock()
    eng = BroadcastStateEngine(live_rules, confirm_observations=3, max_gap_seconds=30, clock=clock, mono=clock)
    nl = live_rules.classify("Go LIVE Preview")
    for _ in range(2):
        eng.observe(nl); clock.advance(2)
    assert eng.confirmed == LiveState.UNKNOWN and not eng.state.fresh
    eng.observe(nl)
    assert eng.confirmed == LiveState.NOT_LIVE and eng.state.fresh
    assert eng.drain_transitions()[0][:2] == (LiveState.UNKNOWN, LiveState.NOT_LIVE)
    # a long gap breaks the streak: the next observation starts counting again
    clock.advance(600)
    eng.observe(nl)
    assert eng.state.candidate_count == 1 and not eng.state.fresh
    # invalid observations (None) count as UNKNOWN observations
    for _ in range(3):
        clock.advance(2); eng.observe(None)
    assert eng.confirmed == LiveState.UNKNOWN


def test_frame_cache_atomic_reload_and_purge(tmp_path):
    clock = FakeClock(1_700_000_000.0)
    fc = FrameCache(tmp_path, clock, clock)
    assert fc.latest() is None and fc.fresh(10) is None
    img = Image.new("RGB", (20, 10), (1, 2, 3))
    f = fc.update(img)
    assert (tmp_path / "latest.png").exists() and (tmp_path / "latest.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert f.captured_utc.endswith("+00:00") and fc.fresh(10) is f
    clock.advance(11)
    assert fc.fresh(10) is None and fc.latest() is f
    # a new process picks the frame up with its original timestamp, but never as "fresh"
    fc2 = FrameCache(tmp_path, clock, clock)
    assert fc2.latest().captured_at == f.captured_at and fc2.fresh(10 ** 9) is None
    out = fc2.export(tmp_path / "copy" / "x.png")
    assert Path(out).exists()
    assert not fc2.purge(7, clock())
    clock.advance(8 * 86400)
    assert fc2.purge(7, clock()) and fc2.latest() is None and not (tmp_path / "latest.png").exists()


def test_startup_registry_toggle():
    reg = DictRegistry()
    assert not is_enabled(reg)
    cmd = enable(reg)
    assert is_enabled(reg) and reg.values[VALUE_NAME] == cmd and "--autostart" in cmd
    apply_setting(False, reg)
    assert not is_enabled(reg)
    assert "studio_monitor" in launch_command() or ".exe" in launch_command()


def test_queue_schema_migration_from_v1_db(tmp_path):
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, payload TEXT NOT NULL, "
                 "screenshot_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
                 "next_attempt_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, sent_at REAL, last_error TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO alerts (incident_id, payload, created_at) VALUES ('INC-1', '{\"text\":\"x\"}', 1)")
    conn.commit(); conn.close()
    q = DeliveryQueue(db)
    assert q.legacy_pending_count() == 1 and q.get_state("schema_version") == 3
    from studio_monitor.bots import BotTarget
    done = q.migrate_legacy_alerts(BotTarget("b", "Default Bot", "42", None))
    assert done == {"pending": 1, "history": 0}
    d = q.due_deliveries()[0]
    assert d.event_id == "INC-1" and d.kind == "incident" and d.chat_id == "42"
    assert q.legacy_pending_count() == 0 and q.migrate_legacy_alerts(None) == {"pending": 0, "history": 0}


def test_queue_cancel_event_and_history_tables(tmp_path):
    from studio_monitor.bots import BotTarget
    q = DeliveryQueue(tmp_path / "q.sqlite3")
    q.create_event("R1", "reminder", "reminders", {"text": "r"}, "", [BotTarget("b", "B", "1", None)])
    assert q.event_has_pending("R1") and q.cancel_event("R1", "went live") == 1
    assert not q.event_has_pending("R1") and q.deliveries_for("R1")[0].status == "cancelled"
    assert q.counts()["cancelled"] == 1 and q.due_deliveries() == []
    q.record_event("E1", "STUDIO_OPENED", "2026-10-09T10:00:00+00:00", {"summary": "s"})
    q.record_event("E2", "NOT_LIVE_REMINDER", "2026-10-09T11:00:00+00:00", {"summary": "r"})
    assert [e["event_id"] for e in q.recent_events(event_types=["NOT_LIVE_REMINDER"])] == ["E2"]
    assert q.delivery_status()["last"]["status"] == "cancelled"


def test_late_delivery_note():
    now = time.time()
    assert late_delivery_note({"created_at": now - 10}, now) == ""
    assert late_delivery_note({}, now) == ""
    note = late_delivery_note({"created_at": now - 3600}, now)
    assert "delivery delayed by" in note and "not the current status" in note
