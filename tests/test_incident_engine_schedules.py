"""Milestone 1: event contracts, durable incident engine, schedules, maintenance."""
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from conftest import FakeClock

from studio_monitor.contracts.events import SCHEMA_VERSION, Event, EvidenceRef, Severity, validate_event
from studio_monitor.incident_engine import DEFAULT_ESCALATION, IncidentEngine, OPEN, RESOLVED
from studio_monitor.schedules import MaintenanceWindow, Schedule


# ---------------------------------------------------------------- events

def test_event_roundtrip_and_defaults(tmp_path):
    shot = tmp_path / "e.png"; shot.write_bytes(b"\x89PNG....")
    ev = Event(device_id="dev-1", type="VERIFICATION", summary="puzzle", session_id="SES-1", owner_label="Roy’s Live",
               account="@roy", evidence=EvidenceRef.from_file(str(shot), "2026-10-09T10:00:00+00:00"))
    assert ev.severity == Severity.URGENT and ev.schema_version == SCHEMA_VERSION and len(ev.event_id) == 36
    back = Event.from_json(ev.to_json())
    assert back == ev and back.evidence.sha256 and back.evidence.size == 8
    assert validate_event(ev.to_dict()) == []


@pytest.mark.parametrize("mutation,needle", [
    ({"event_id": "nope"}, "UUID"), ({"schema_version": 7}, "schema_version"), ({"device_id": ""}, "device_id"),
    ({"type": "WHATEVER"}, "unknown event type"), ({"severity": "LOUD"}, "severity"), ({"observed_utc": "yesterday"}, "ISO-8601"),
    ({"validity": "maybe"}, "validity"), ({"detail": "x"}, "detail must be an object"),
])
def test_event_validation_rejects(mutation, needle):
    d = Event(device_id="d", type="TEST", summary="s").to_dict()
    d.update(mutation)
    errs = validate_event(d)
    assert errs and any(needle in e for e in errs)
    with pytest.raises(ValueError):
        Event.from_dict(d)


def test_event_too_large_rejected():
    d = Event(device_id="d", type="TEST", summary="s", detail={"blob": "x" * 300_000}).to_dict()
    assert any("too large" in e for e in validate_event(d))


# ---------------------------------------------------------------- incident engine

@pytest.fixture
def eng(tmp_path, clock):
    conn = sqlite3.connect(tmp_path / "inc.sqlite3", check_same_thread=False, isolation_level=None)
    return IncidentEngine(conn, clock=clock, coalesce_seconds=120)


def test_one_incident_per_problem_episode_with_coalescing(eng, clock):
    a = eng.open_or_update("dev", "restrictions", "restriction_notice", Severity.URGENT, "restricted", session_id="S1")
    assert a.is_new and a.incident.status == OPEN and a.incident.reminders_sent == 0
    clock.advance(30)
    b = eng.open_or_update("dev", "restrictions", "restriction_notice", Severity.URGENT, "restricted again", session_id="S1")
    assert not b.is_new and b.coalesced and b.incident.incident_id == a.incident.incident_id and b.incident.occurrences == 2
    clock.advance(600)
    c = eng.open_or_update("dev", "restrictions", "restriction_notice", Severity.URGENT, "still", session_id="S1")
    assert not c.coalesced and c.incident.occurrences == 3
    tl = eng.timeline(a.incident.incident_id)
    assert [t["kind"] for t in tl] == ["opened", "update"] and tl[0]["coalesced"] == 2   # repeat folded into the opening entry
    # a different problem or a different device is an independent episode
    d = eng.open_or_update("dev", "restrictions", "content_warning", Severity.WARNING, "warn", session_id="S1")
    e = eng.open_or_update("dev2", "restrictions", "restriction_notice", Severity.URGENT, "other pc", session_id="S9")
    assert d.is_new and e.is_new and len(eng.list(status=OPEN)) == 3
    # after resolution a new occurrence is a new episode
    eng.resolve(a.incident.incident_id, "restriction popup no longer visible (this does not mean the restriction was lifted)")
    f = eng.open_or_update("dev", "restrictions", "restriction_notice", Severity.URGENT, "back", session_id="S1")
    assert f.is_new and f.incident.incident_id != a.incident.incident_id
    assert eng.get(a.incident.incident_id).status == RESOLVED and "no longer visible" in eng.get(a.incident.incident_id).resolution


def test_acknowledgement_pauses_escalation_but_keeps_open(eng, clock):
    inc = eng.open_or_update("dev", "verification", "verification_puzzle", Severity.URGENT, "puzzle").incident
    assert inc.next_reminder_at == pytest.approx(clock() + DEFAULT_ESCALATION[Severity.URGENT][0])
    clock.advance(301)
    due = eng.escalations_due()
    assert len(due) == 1 and due[0].sequence == 1
    acked = eng.acknowledge(inc.incident_id, actor="tg:12345", note="handling it")
    assert acked.acknowledged and acked.status == OPEN and acked.next_reminder_at is None
    assert eng.escalations_due() == []
    assert eng.claim_escalation(inc.incident_id, due[0].claim_seq) is None     # race: ack won


def test_escalation_claim_race_and_limits(eng, clock):
    inc = eng.open_or_update("dev", "verification", "verification_puzzle", Severity.URGENT, "puzzle").incident
    clock.advance(301)
    due = eng.escalations_due()[0]
    first = eng.claim_escalation(inc.incident_id, due.claim_seq)
    assert first is not None and first.reminders_sent == 1
    assert eng.claim_escalation(inc.incident_id, due.claim_seq) is None       # second worker loses the claim
    clock.advance(301); eng.claim_escalation(inc.incident_id, eng.escalations_due()[0].claim_seq)
    clock.advance(301); last = eng.claim_escalation(inc.incident_id, eng.escalations_due()[0].claim_seq)
    assert last.reminders_sent == 3 and last.next_reminder_at is None          # max reminders reached
    clock.advance(3600)
    assert eng.escalations_due() == []
    kinds = [t["kind"] for t in eng.timeline(inc.incident_id)]
    assert kinds.count("reminder") == 3


def test_resolution_cancels_pending_escalation(eng, clock):
    inc = eng.open_or_update("dev", "restrictions", "restriction_notice", Severity.URGENT, "r").incident
    clock.advance(301)
    due = eng.escalations_due()[0]
    eng.resolve(inc.incident_id, "popup no longer visible")
    assert eng.claim_escalation(inc.incident_id, due.claim_seq) is None and eng.escalations_due() == []


def test_info_severity_has_no_escalation(eng):
    inc = eng.open_or_update("dev", "studio_opened", "opened", Severity.INFO, "opened").incident
    assert inc.next_reminder_at is None


def test_snooze_scopes_and_expiry(eng, clock):
    inc = eng.open_or_update("dev", "audio", "silence", Severity.WARNING, "silence").incident
    eng.snooze("incident", inc.incident_id, 3600, actor="tg:1", reason="known")
    assert eng.is_suppressed("dev", "audio", inc.incident_id) and not eng.is_suppressed("dev", "face")
    clock.advance(DEFAULT_ESCALATION[Severity.WARNING][0] + 1)
    assert eng.escalations_due() == []                                        # snoozed incidents do not escalate
    clock.advance(3600)
    assert not eng.is_suppressed("dev", "audio", inc.incident_id)
    assert len(eng.escalations_due()) == 1                                     # escalation resumes after the snooze
    eng.snooze("device", "dev", 300)
    assert eng.is_suppressed("dev", "anything") and not eng.is_suppressed("dev2", "anything")
    eng.snooze("category", "dev2:face", 300)
    assert eng.is_suppressed("dev2", "face") and not eng.is_suppressed("dev2", "audio")


def test_maintenance_keeps_critical_categories(eng, clock):
    until = eng.enter_maintenance("dev", 1800, ["face", "audio", "restrictions", "verification"], reason="camera swap")
    m = eng.maintenance("dev")
    assert m and sorted(m["categories"]) == ["audio", "face"] and m["remaining_seconds"] == pytest.approx(1800)
    assert eng.is_suppressed("dev", "face") and not eng.is_suppressed("dev", "verification")
    clock.advance(1801)
    assert eng.maintenance("dev") is None and not eng.is_suppressed("dev", "face")
    eng.enter_maintenance("dev", 600, ["restrictions"], keep_critical=False)
    assert eng.is_suppressed("dev", "restrictions")
    assert eng.exit_maintenance("dev") and eng.maintenance("dev") is None


def test_root_messages_per_destination_and_session_summary(eng, clock):
    inc = eng.open_or_update("dev", "restrictions", "restriction_notice", Severity.URGENT, "r", session_id="S1").incident
    eng.set_root_message(inc.incident_id, "botA", "11", None, 501)
    eng.set_root_message(inc.incident_id, "botB", "-100", 7, 77)
    assert eng.root_message(inc.incident_id, "botA", "11") == 501 and eng.root_message(inc.incident_id, "botB", "-100") == 77
    assert eng.root_message(inc.incident_id, "botC", "1") is None
    clock.advance(120)
    eng.resolve(inc.incident_id, "no longer visible")
    eng.open_or_update("dev", "face", "absent", Severity.WARNING, "no face", session_id="S1")
    s = eng.session_summary("dev", "S1")
    assert s["incidents"] == 2 and s["by_category"]["restrictions"]["resolved"] == 1
    assert s["by_category"]["restrictions"]["total_seconds"] == pytest.approx(120, abs=1)
    assert s["by_category"]["face"]["open"] == 1


def test_concurrent_claims_are_safe(eng, clock):
    inc = eng.open_or_update("dev", "verification", "verification_puzzle", Severity.URGENT, "p").incident
    clock.advance(301)
    due = eng.escalations_due()[0]
    wins = []

    def worker():
        if eng.claim_escalation(inc.incident_id, due.claim_seq) is not None:
            wins.append(1)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(wins) == 1


# ---------------------------------------------------------------- schedules

def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_schedule_window_grace_and_missed_start():
    s = Schedule(enabled=True, timezone="Africa/Nairobi", weekdays=["mon", "tue", "wed", "thu", "fri"], start="20:00", end="23:00",
                 grace_minutes=15)
    assert s.validate() == []
    # Friday 2026-10-09 20:30 Nairobi == 17:30 UTC
    now = utc(2026, 10, 9, 17, 30)
    win = s.window_at(now)
    assert win and win[0] == utc(2026, 10, 9, 17, 0) and win[1] == utc(2026, 10, 9, 20, 0)
    assert s.missed_start(now, "NOT_LIVE") and not s.missed_start(now, "UNKNOWN") and not s.missed_start(now, "LIVE")
    assert not s.missed_start(utc(2026, 10, 9, 17, 10), "NOT_LIVE")           # inside grace
    assert s.window_at(utc(2026, 10, 10, 17, 30)) is None                      # Saturday: no window
    assert not s.reminders_allowed(utc(2026, 10, 9, 12, 0)) and s.reminders_allowed(now)
    s.exceptions = ["2026-10-09"]
    assert s.window_at(now) is None
    assert Schedule().reminders_allowed(now)                                   # disabled schedule never gates


def test_overnight_window_and_dst_transitions():
    # Berlin: DST ends 2026-10-25 03:00 -> 02:00. Overnight window 22:00-02:00 starting Saturday 24th.
    s = Schedule(enabled=True, timezone="Europe/Berlin", weekdays=["sat"], start="22:00", end="02:00", grace_minutes=10)
    start_utc = utc(2026, 10, 24, 20, 0)          # 22:00 CEST
    win = s.window_at(utc(2026, 10, 24, 23, 30))  # 01:30 CEST on the 25th, still inside
    assert win and win[0] == start_utc
    # 02:00 local on the 25th is ambiguous during the fold; the window ends at the first 02:00 (CEST) = 00:00 UTC
    assert win[1] == utc(2026, 10, 25, 0, 0)
    assert s.window_at(utc(2026, 10, 25, 0, 30)) is None
    # Spring forward: 2026-03-29 02:00 -> 03:00 Berlin. Window on Saturday 28th 22:00-02:00 ends 01:00 UTC.
    # 02:00 local does not exist that night; zoneinfo resolves it with the pre-transition offset (CET) -> 01:00 UTC
    win2 = s.window_at(utc(2026, 3, 28, 22, 0))
    assert win2 and win2[1] == utc(2026, 3, 29, 1, 0)
    nxt = s.next_window(utc(2026, 10, 20, 12, 0))
    assert nxt and nxt[0] == start_utc


def test_schedule_validation_and_dict():
    bad = Schedule(enabled=True, timezone="Mars/Olympus", start="25:00", weekdays=[], exceptions=["soon"])
    errs = bad.validate()
    assert len(errs) == 4
    s = Schedule.from_dict({"enabled": True, "timezone": "UTC", "weekdays": ["mon", "xyz"], "start": "09:00", "end": "10:00"})
    assert s.weekdays == ["mon"] and Schedule.from_dict(s.to_dict()) == s


def test_maintenance_window_dataclass():
    m = MaintenanceWindow(until_utc=utc(2026, 10, 9, 12, 0).isoformat(), categories=["face"], reason="break")
    assert m.active(utc(2026, 10, 9, 11, 0)) and not m.active(utc(2026, 10, 9, 12, 1))
    assert m.remaining(utc(2026, 10, 9, 11, 30)).total_seconds() == 1800
