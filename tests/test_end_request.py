"""End-LIVE confirmation dialog ("End streaming?"): detection, one alert per
dialog episode, outcomes (ended / continued / unknown), restart
reconciliation, standalone vs managed routing, masked evidence.

Real Studio evidence: the operator's dialog crop is a private, git-ignored
fixture (tests/fixtures/private/); the committed fixture is synthetic."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeClock, all_deliveries, make_window
from studio_monitor.config import AppConfig
from studio_monitor.credentials import MemoryCredentialStore
from studio_monitor.end_request import STATE_KEY, EndDialogRules, EndRequestTracker
from studio_monitor.regions import Region
from test_activity import Harness, LIVE_TEXT, NOT_LIVE_TEXT

RULES = Path(__file__).resolve().parents[1] / "rules" / "end_dialog_rules.json"
FIXTURE = Path(__file__).with_name("fixtures") / "end_streaming_dialog_synthetic.png"
PRIVATE = Path(__file__).with_name("fixtures") / "private" / "end_streaming_real.png"
DIALOG_LINES = ["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now", "Cancel"]
DIALOG_TEXT = LIVE_TEXT + "\n" + "\n".join(DIALOG_LINES)


@pytest.fixture
def rules_end():
    return EndDialogRules.load(RULES)


# ---------------------------------------------------------------- matching

def test_matches_real_heading_and_button_combination(rules_end):
    m = rules_end.match("\n".join(DIALOG_LINES), DIALOG_LINES)
    assert m and m["heading"] == "end streaming" and m["confirm"] == "end now" and m["cancel"] == "cancel"
    # inside a whole Studio frame (lines around it)
    lines = ["TikTok LIVE Studio", "Scenes", "LIVE 00:12:34", "1,204 viewers", *DIALOG_LINES, "Chat", "Add source"]
    assert rules_end.match("\n".join(lines), lines)
    # text-only OCR (no line structure) within the character window
    assert rules_end.match("Scenes End streaming? End LIVE? Share your LIVE for more viewers. End now Cancel Chat")


def test_rejects_isolated_words_and_unrelated_dialogs(rules_end):
    assert rules_end.match(LIVE_TEXT, LIVE_TEXT.split("  ")) is None                        # "End LIVE" control alone
    assert rules_end.match("End now", ["End now"]) is None
    assert rules_end.match("End streaming?", ["End streaming?"]) is None                     # heading alone
    assert rules_end.match("End streaming? Keep going", ["End streaming?", "Keep going"]) is None
    assert rules_end.match("Are you sure you want to exit?\nEnd now\nCancel", ["Are you sure you want to exit?", "End now", "Cancel"]) is None
    far = ["End streaming?"] + [f"line {i}" for i in range(12)] + ["End now", "Cancel"]
    assert rules_end.match("\n".join(far), far) is None                                      # not spatially related
    assert rules_end.match("End streaming?" + " filler" * 60 + " End now Cancel") is None   # beyond the char window


@pytest.mark.skipif(not FIXTURE.exists(), reason="synthetic fixture missing")
def test_synthetic_fixture_matches_through_real_windows_ocr(rules_end):
    try:
        from studio_monitor.ocr import create_backend
        ocr = create_backend("windows", "en", 1.0)
        res = ocr.recognize(Image.open(FIXTURE).convert("RGB"))
    except Exception as exc:  # pragma: no cover - machine without Windows OCR
        pytest.skip(f"Windows OCR unavailable: {exc}")
    assert rules_end.match(res.text, res.lines), res.lines


@pytest.mark.skipif(not PRIVATE.exists(), reason="private real-dialog fixture not present on this machine")
def test_private_real_dialog_crop_matches(rules_end):
    from studio_monitor.ocr import create_backend
    res = create_backend("windows", "en", 1.0).recognize(Image.open(PRIVATE).convert("RGB"))
    assert rules_end.match(res.text, res.lines), res.lines


# ---------------------------------------------------------------- tracker unit

def test_tracker_confirmation_window_and_invalid_frames(rules_end):
    clock = FakeClock()
    state = {}
    t = EndRequestTracker(rules_end, lambda k, d=None: state.get(k, d), state.__setitem__, clock)
    view = [(DIALOG_TEXT, [])]
    assert t.observe(view, True, "LIVE", True, True, "EP1", "S1") == []            # 1st frame: not yet confirmed
    clock.advance(2)
    ev = t.observe(view, True, "LIVE", True, True, "EP1", "S1")
    assert [e.kind for e in ev] == ["opened"] and t.dialog_open and state[STATE_KEY]["outcome"] == ""
    for _ in range(20):                                                             # invalid capture: never "disappearance"
        clock.advance(2)
        assert t.observe([], False, "UNKNOWN", False, True) == []
    assert t.dialog_open
    clock.advance(2)
    assert t.observe([(LIVE_TEXT, [])], True, "LIVE", True, True) == []             # one miss is not closure yet
    clock.advance(2)
    ev = t.observe([(LIVE_TEXT, [])], True, "LIVE", True, True)                     # second miss -> closed, LIVE fresh -> continued
    assert [e.kind for e in ev] == ["continued"] and t.current is None and state[STATE_KEY]["outcome"] == "continued"


# ---------------------------------------------------------------- monitor scenarios

def live_harness(cfg, rules, clock, **kw):
    h = Harness(cfg, rules, clock, **kw)
    h.ocr.default = LIVE_TEXT
    h.run(30)
    assert h.mon.broadcast.state.state.value == "LIVE"
    return h


def end_alerts(h):
    return [d for d in all_deliveries(h.queue) if d["event_id"].startswith("END-") and d["event_id"].count("-") == 3]


def follow_ups(h, suffix):
    return [d for d in all_deliveries(h.queue) if d["event_id"].startswith("END-") and d["event_id"].endswith(suffix)]


def test_one_alert_for_persistent_dialog_with_labels_and_masked_screenshot(cfg, rules, clock):
    cfg.owner_name = "Roy"
    cfg.regions = [Region("mask", 0.0, 0.0, 0.2, 0.2, kind="redact")]
    h = live_harness(cfg, rules, clock)
    h.ocr.default = DIALOG_TEXT
    h.run(60)
    alerts = end_alerts(h)
    assert len(alerts) == 1
    text = alerts[0]["payload"]["text"]
    assert "Roy’s Live — END-LIVE CONFIRMATION OPENED" in text and "TikTok account: unavailable" in text
    assert "not yet been confirmed ended" in text and "Observed:" in text
    shot = alerts[0]["screenshot_path"]
    assert shot and Path(shot).exists()
    img = Image.open(shot).convert("RGB")
    assert img.getpixel((5, 5)) == (0, 0, 0)                                        # privacy mask applied to the evidence
    # broadcast state untouched; no report, no offline episode
    assert h.mon.broadcast.state.state.value == "LIVE" and h.mon.episodes.state.episode_id
    assert not [d for d in all_deliveries(h.queue) if d["event_id"].startswith("RPT-")]
    assert h.mon.reminders.state.episode_id == ""
    inc = h.mon.incident_engine.get(alerts[0]["event_id"])
    assert inc is not None and inc.is_open and inc.severity == "INFO"
    assert h.mon.activity.end_request["dialog_visible"] is True
    # detectors and username lookup are held while the dialog is open
    assert h.mon._blocking_detection is True
    snap = h.mon.end_request_snapshot()
    assert snap["alerted"] is True


def test_alert_uses_verified_username_when_available(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    from studio_monitor.account import SUCCEEDED
    a = h.mon.account
    a.status, a.username, a.episode_id = SUCCEEDED, "roy_live", h.mon.episodes.state.episode_id
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    assert "TikTok account: @roy_live" in end_alerts(h)[0]["payload"]["text"]


def test_dialog_closed_live_continues_then_reopen_is_new_episode(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    first = end_alerts(h)
    assert len(first) == 1
    h.ocr.default = LIVE_TEXT
    h.run(10)
    cont = follow_ups(h, "-CONT")
    assert len(cont) == 1 and "LIVE CONTINUES" in cont[0]["payload"]["text"] and cont[0]["payload"]["thread_of"] == first[0]["event_id"]
    assert "Cancel" not in cont[0]["payload"]["text"].replace("End confirmation", "")       # never claims a click was observed
    assert not h.mon.incident_engine.get(first[0]["event_id"]).is_open
    assert h.mon.broadcast.state.state.value == "LIVE"
    h.ocr.default = DIALOG_TEXT                                                     # reopened -> new episode, new alert
    h.run(10)
    alerts = end_alerts(h)
    assert len(alerts) == 2 and alerts[1]["event_id"] != first[0]["event_id"]


def test_confirmed_end_produces_one_final_event_and_one_report(cfg, rules, clock):
    cfg.activity.session_reports = True
    h = live_harness(cfg, rules, clock)
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    alert = end_alerts(h)[0]
    h.ocr.default = NOT_LIVE_TEXT                                                   # operator confirmed; Studio shows Go LIVE
    h.run(60)
    ended = follow_ups(h, "-END")
    assert len(ended) == 1 and "LIVE HAS ENDED" in ended[0]["payload"]["text"] and ended[0]["payload"]["thread_of"] == alert["event_id"]
    reports = [d for d in all_deliveries(h.queue) if d["event_id"].startswith("RPT-")]
    assert len(reports) == 1 and "End-request dialog" in reports[0]["payload"]["text"] and "ended" in reports[0]["payload"]["text"]
    assert follow_ups(h, "-CONT") == []
    ep = h.mon.end_requests.last
    assert ep.outcome == "ended" and ep.confirmed_end_utc
    h.run(120)
    assert len(follow_ups(h, "-END")) == 1 and len(reports) == 1                   # nothing duplicated later
    hist = [e for e in h.queue.recent_events(50) if e["event_type"] == "BROADCAST_END_REQUESTED"]
    assert {e["details"].get("outcome", "open") for e in hist} >= {"open", "ended"}


def test_broadcast_can_end_without_the_dialog(cfg, rules, clock):
    cfg.activity.session_reports = True
    h = live_harness(cfg, rules, clock)
    h.ocr.default = NOT_LIVE_TEXT
    h.run(60)
    assert end_alerts(h) == [] and follow_ups(h, "-END") == []
    assert len([d for d in all_deliveries(h.queue) if d["event_id"].startswith("RPT-")]) == 1


def test_capture_loss_and_studio_exit_are_not_confirmation(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    alert = end_alerts(h)[0]
    h.sys.windows[0x1001] = make_window(minimized=True)                             # no frames at all
    h.run(120)
    assert follow_ups(h, "-END") == [] and follow_ups(h, "-CONT") == []
    assert h.mon.end_requests.current is not None                                   # still pending, honestly
    h.close_studio()                                                                # process exits
    h.run(40)
    ep = h.mon.end_requests.last
    assert ep is not None and ep.outcome == "unknown" and "Studio exited" in ep.outcome_reason
    assert follow_ups(h, "-END") == []                                              # no end claim from exit alone
    inc = h.mon.incident_engine.get(alert["event_id"])
    assert not inc.is_open and "unknown" in (inc.resolution or "")
    assert any(e["event_type"] == "STUDIO_CLOSED" for e in h.queue.recent_events(50))


def test_unknown_after_bounded_wait_without_evidence(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    h.sys.windows[0x1001] = make_window(minimized=True)
    h.run(200)                                                                      # > resolve_timeout (180 s) without any frame
    ep = h.mon.end_requests.last
    assert ep is not None and ep.outcome == "unknown" and "no valid capture" in ep.outcome_reason


def test_restart_reconciles_with_fresh_frames(cfg, rules, clock):
    h = live_harness(cfg, rules, clock)
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    alert = end_alerts(h)[0]
    saved = h.queue.get_state(STATE_KEY)
    assert saved["alerted"] and saved["outcome"] == ""
    # restart while the dialog is still open: no second alert
    h2 = Harness(cfg, rules, clock, queue=h.queue)
    h2.ocr.default = DIALOG_TEXT
    assert h2.mon.end_requests.current is not None and h2.mon.end_requests.current.reconciling
    h2.run(20)
    assert len(end_alerts(h2)) == 1
    # restart after the dialog was closed and LIVE continues: outcome follows fresh frames, still no new alert
    h3 = Harness(cfg, rules, clock, queue=h.queue)
    h3.ocr.default = LIVE_TEXT
    h3.run(40)
    assert len(end_alerts(h3)) == 1 and len(follow_ups(h3, "-CONT")) == 1
    assert h3.mon.end_requests.current is None
    # a restart with the dialog gone and no frames at all resolves to unknown only after the bounded wait
    h.ocr.default = DIALOG_TEXT
    h4 = Harness(cfg, rules, clock, queue=h.queue)
    h4.ocr.default = DIALOG_TEXT
    h4.run(10)
    assert len(end_alerts(h4)) == 2
    h5 = Harness(cfg, rules, clock, queue=h.queue)
    h5.sys.windows[0x1001] = make_window(minimized=True)
    h5.run(200)
    assert h5.mon.end_requests.last.outcome == "unknown" and "restart" in h5.mon.end_requests.last.outcome_reason


def test_managed_routing_mirrors_without_local_delivery(hubfx_factory, cfg, rules, clock):
    hubfx, tr = hubfx_factory()
    from test_hub_agent import managed_harness
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    h, sync = managed_harness(hubfx, cfg, rules, clock)
    h.ocr.default = LIVE_TEXT
    h.run(30)
    h.ocr.default = DIALOG_TEXT
    h.run(10)
    assert end_alerts(h) == []                                                       # hub owns delivery
    evs = hubfx.events(cfg.device.device_id)
    assert any(t == "BROADCAST_END_REQUESTED" for t, _s, _e in evs)
    from hub.db import EventRow
    with hubfx.app.state.session_factory() as s:
        row = s.query(EventRow).filter_by(type="BROADCAST_END_REQUESTED").one()
        assert row.observed_utc == h.mon.end_requests.current.opened_utc           # detection time preserved on the hub
        assert row.evidence_stored_path                                             # masked screenshot uploaded
    assert hubfx.devices()[0]["status"] == "LIVE"                                   # fleet broadcast state unchanged
    incs = hubfx.incidents()
    assert any(i["type"] == "BROADCAST_END_REQUESTED" for i in incs)
    h.ocr.default = LIVE_TEXT
    h.run(10)
    assert [t for t, _s, _e in hubfx.events(cfg.device.device_id)].count("INCIDENT_RESOLVED") >= 1
    assert all(i["type"] != "BROADCAST_END_REQUESTED" for i in hubfx.incidents())


@pytest.fixture
def hubfx_factory(tmp_path, clock):
    from test_commands import TgTransport
    from test_hub_agent import HubFixture
    created = []

    def make():
        tr = TgTransport()
        fx = HubFixture(tmp_path, clock)
        fx.app.state.worker.transport = tr
        fx.app.state.background.commands.transport = tr
        created.append(fx)
        return fx, tr
    yield make
    for fx in created:
        fx.tc.__exit__(None, None, None)
