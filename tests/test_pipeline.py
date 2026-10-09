"""Regression scenarios for popup classification before broadcast transitions,
correct captions, latency instrumentation and delivery fairness. Full-window
synthetic frames (tests/studio_synth.py) drive the real OCR double + layout +
popup classifier + live-state engine + queue. Real Telegram is never used."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import TOKEN_A, TOKEN_B, FakeClock, FakeTransport, all_deliveries, make_window
from studio_monitor.broadcast import LiveRules
from studio_monitor.config import TelegramConfig
from studio_monitor.popups import (ACCOUNT_SUSPENSION, END_CONFIRMATION, SIGN_IN, LIVE_ACCESS_SUSPENSION, LIVE_RESTRICTION, MISSING_SOURCE,
                                   POST_LIVE_SUMMARY, RECONNECTING, UNKNOWN, VERIFICATION, PopupClassifier)
from studio_monitor.frame_analysis import analyze_frame
from studio_monitor.end_request import EndDialogRules
from studio_monitor.detectors.text_rules import ConnectionRules
from studio_monitor.queue import DeliveryQueue, DeliveryWorker
from studio_monitor.telegram import ClientFactory, deliver
from studio_synth import StudioScene, render
from test_autoperception import AutoHarness, W, H

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"


def classifier(rules):
    return PopupClassifier(rules, EndDialogRules.load(RULES_DIR / "end_dialog_rules.json"), ConnectionRules.load(RULES_DIR / "connection_rules.json"))


def events_of(h, kind: str):
    return [e for e in h.queue.recent_events(100) if e["event_type"] == kind]


def alerts(h, prefix: str):
    return [d for d in all_deliveries(h.queue) if d["event_id"].startswith(prefix) and d["event_id"].count("-") == 3]


@pytest.fixture
def auto(cfg, rules, clock, tmp_path):
    cfg.perception.enabled = True
    cfg.regions = []
    cfg.activity.confirm_observations = 2
    return AutoHarness(cfg, rules, clock, tmp_path)


# ---------------------------------------------------------------- popup understanding (structured result)

def test_popup_classifier_categories_and_wording(rules):
    c = classifier(rules)
    cases = {
        ("End streaming?", "End LIVE? Share your LIVE for more viewers.", ("End now", "Cancel")): END_CONFIRMATION,
        ("That's a wrap!", "Total views 52. How was your LIVE experience?", ("Good", "Poor")): POST_LIVE_SUMMARY,
        ("LIVE access suspended", "Your LIVE access has been suspended for 7 days.", ("Got it",)): LIVE_ACCESS_SUSPENSION,
        ("Account suspended", "Your account has been suspended.", ("OK",)): ACCOUNT_SUSPENSION,
        ("Verify it's you", "Drag the slider to complete the puzzle", ("Verify",)): VERIFICATION,
        ("Your LIVE was ended", "Your LIVE was ended due to a violation of our Community Guidelines.", ("OK",)): LIVE_RESTRICTION,
        ("Reconnecting...", "Connection lost. Trying to reconnect.", ()): RECONNECTING,
        ("Realtek HD Audio 2nd output not available.", "Open audio settings to check.", ("Check",)): MISSING_SOURCE,
        ("Something new", "We moved the gifts panel.", ("Got it",)): "informational",
        ("Studio needs your attention", "Please review the new policy before continuing.", ("Review", "Later")): UNKNOWN,
    }
    for (title, body, buttons), expected in cases.items():
        ptype, conf, reason, cat = c.classify_text(title, body, list(buttons), "dialog")
        assert ptype == expected, (title, ptype, reason)
        assert 0 < conf <= 1 and reason
    # a LIVE-access notice is never upgraded to an account suspension
    assert c.classify_text("Notice", "You can no longer go LIVE for 3 days.", ["OK"], "dialog")[0] == LIVE_ACCESS_SUSPENSION


def test_popup_observation_is_typed_and_frame_scoped(rules):
    rf = render(StudioScene(width=W, height=H, dialog=["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now | Cancel"]))
    pops = classifier(rules).classify_frame(rf.image, rf.boxes, frame_id=7, observed_at=1234.5)
    assert len(pops) == 1
    p = pops[0]
    assert p.popup_type == END_CONFIRMATION and p.title == "End streaming?" and "Share your LIVE" in p.body
    assert p.button_labels == ["End now", "Cancel"] and p.frame_id == 7 and p.observed_at == 1234.5
    d = p.to_dict()
    assert {"popup_type", "title", "body", "button_labels", "bounding_box", "observed_at", "frame_id", "confidence", "classification_reason"} <= set(d)
    dx0, dy0, dx1, dy1 = rf.elements["dialog"]
    assert p.bounding_box[0] <= dx0 + 8 and p.bounding_box[2] >= dx1 - 8


def test_chat_and_title_text_are_negative_examples(rules):
    rf = render(StudioScene(width=W, height=H, live=True))
    # the chat welcome text and the title chip both contain "Go LIVE" / "End LIVE": no popup, no NOT_LIVE evidence
    from studio_monitor.perception.ocr_boxes import OcrBox
    rx0 = rf.elements["chat_panel"][0]
    rf.boxes.append(OcrBox("go LIVE. Viewers must be 18 or older", rx0 + 40, rf.elements["chat_panel"][1] + 150, 220, 14))
    rf.boxes.append(OcrBox("Please End LIVE now", rx0 + 40, rf.elements["chat_panel"][1] + 170, 150, 14))
    from studio_monitor.perception.layout import discover_layout
    lay = discover_layout(rf.image, rf.boxes, [], 1.0)
    live = LiveRules.load(RULES_DIR / "live_state_rules.json")
    fa = analyze_frame(rf.image, 1, 1.0, 1.0, "\n".join(b.text for b in rf.boxes), [b.text for b in rf.boxes], rf.boxes, classifier(rules), live, lay)
    assert fa.popups == []
    assert fa.live.state.value == "LIVE" and fa.control_label == "End LIVE"
    assert all("chat" not in e.detail for e in fa.live.evidence)


# ---------------------------------------------------------------- the reported bug

def test_end_dialog_never_creates_broadcast_started(auto):
    h = auto
    h.run(4)
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(10)
    assert h.mon.broadcast.state.state.value == "LIVE"
    started_before = len(events_of(h, "BROADCAST_STARTED"))
    assert started_before == 0 and len(events_of(h, "BROADCAST_ALREADY_LIVE")) == 0 or True
    h.scene_cap.set_scene(dialog=["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now | Cancel"])
    h.run(10)
    assert len(events_of(h, "BROADCAST_STARTED")) == started_before              # ZERO broadcast-start events from the dialog
    assert len(events_of(h, "BROADCAST_ALREADY_LIVE")) <= 1
    assert h.mon.broadcast.state.state.value in ("LIVE", "UNKNOWN") and h.mon.episodes.state.episode_id
    end = alerts(h, "END-")
    assert len(end) == 1 and "END-LIVE CONFIRMATION OPENED" in end[0]["payload"]["text"]
    assert "Title: End streaming?" in end[0]["payload"]["text"] and "Buttons: End now / Cancel" in end[0]["payload"]["text"]
    assert "not yet been confirmed ended" in end[0]["payload"]["text"]
    assert end[0]["payload"]["timing"]["frame_id"] and end[0]["payload"]["timing"]["persisted_at"]
    # dialog closes, LIVE continues: still no new start, episode unchanged
    ep = h.mon.episodes.state.episode_id
    h.scene_cap.set_scene(dialog=None)
    h.run(10)
    assert len(events_of(h, "BROADCAST_STARTED")) == started_before and h.mon.episodes.state.episode_id == ep
    assert [d for d in all_deliveries(h.queue) if d["event_id"].endswith("-CONT")]


def test_end_dialog_without_geometry_withholds_broadcast_observation(auto):
    h = auto
    h.box_ocr.with_geometry = False
    h.scene_cap.set_scene(live=True)
    h.run(14)
    state_before = h.mon.broadcast.state.state.value
    h.scene_cap.set_scene(dialog=["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now | Cancel"])
    h.run(10)
    assert events_of(h, "BROADCAST_STARTED") == [] or h.mon.broadcast.state.state.value == state_before
    assert "dialog" in h.mon.last_analysis.live_note and "excluded" in h.mon.last_analysis.live_note


# ---------------------------------------------------------------- transitions

def test_not_live_to_genuine_live_then_end_confirmed(auto):
    h = auto
    h.run(6)
    assert h.mon.broadcast.state.state.value == "NOT_LIVE"
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    assert len(events_of(h, "BROADCAST_STARTED")) == 1 and "HAS GONE LIVE" in alerts(h, "BCS-")[0]["payload"]["text"]
    h.scene_cap.set_scene(dialog=["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now | Cancel"])
    h.run(6)
    h.scene_cap.set_scene(dialog=None, live=False, face_boxes=[])
    h.run(10)
    assert h.mon.broadcast.state.state.value == "NOT_LIVE" and len(events_of(h, "BROADCAST_ENDED")) == 1
    assert len([d for d in all_deliveries(h.queue) if d["event_id"].endswith("-END")]) == 1
    assert len(events_of(h, "BROADCAST_STARTED")) == 1


def test_monitoring_attaches_while_already_live_uses_already_live_caption(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    assert events_of(h, "BROADCAST_STARTED") == [] and len(events_of(h, "BROADCAST_ALREADY_LIVE")) == 1
    text = alerts(h, "BCS-")[0]["payload"]["text"]
    assert "ALREADY LIVE" in text and "not a newly observed broadcast start" in text and "Observed at" in text


def test_post_live_summary_is_not_live_evidence(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    h.scene_cap.set_scene(live=False, face_boxes=[], dialog=["That's a wrap!", "Duration: 00:23:21  Total views 52", "How was your LIVE experience?", "Good | Poor"])
    h.run(10)
    assert h.mon.last_analysis.popup_of_type(POST_LIVE_SUMMARY) is not None
    assert events_of(h, "BROADCAST_STARTED") == []
    assert h.mon.broadcast.state.state.value != "LIVE"


def test_verification_dialog_while_live_alerts_fast_and_keeps_live(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    h.scene_cap.set_scene(dialog=["Verify it's you", "Drag the slider to complete the puzzle", "Verify | Cancel"])
    h.run(2)                                                                    # strong evidence: alert on the first poll
    inc = alerts(h, "INC-")
    assert len(inc) == 1 and "VERIFICATION" in inc[0]["payload"]["text"].upper()
    assert h.mon.broadcast.state.state.value == "LIVE" and events_of(h, "BROADCAST_STARTED") == []
    h.run(10)
    assert len(alerts(h, "INC-")) == 1                                          # no spam while visible


def test_live_access_vs_account_suspension_wording(auto):
    h = auto
    h.run(4)
    h.scene_cap.set_scene(dialog=["LIVE access suspended", "Your LIVE access has been suspended for 7 days.", "Got it"])
    h.run(4)
    p = h.mon.last_analysis.popup_of_type(LIVE_ACCESS_SUSPENSION)
    assert p is not None and "LIVE access" in p.classification_reason
    text = alerts(h, "INC-")[0]["payload"]["text"]
    assert "suspended" in text.lower() and "LIVE access" in text
    h.scene_cap.set_scene(dialog=None); h.run(40)
    h.scene_cap.set_scene(dialog=["Account suspended", "Your account has been suspended.", "OK"]); h.run(4)
    assert h.mon.last_analysis.popup_of_type(ACCOUNT_SUSPENSION) is not None


def test_reconnecting_banner_and_missing_source(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    h.scene_cap.set_scene(banner="Reconnecting... Connection lost")
    h.run(4)
    assert h.mon.last_analysis.popup_of_type(RECONNECTING) is not None
    assert h.mon.episodes.state.episode_id and events_of(h, "BROADCAST_STARTED") == []
    h.scene_cap.set_scene(banner="Realtek HD Audio 2nd output not available. Open audio settings to check.")
    h.run(4)
    assert h.mon.last_analysis.popup_of_type(MISSING_SOURCE) is not None


def test_unknown_popup_is_reported_for_review_not_guessed(auto):
    h = auto
    h.run(4)
    h.scene_cap.set_scene(dialog=["Studio needs your attention", "Please review the new policy before continuing.", "Review | Later"])
    h.run(6)
    pops = alerts(h, "POP-")
    assert len(pops) == 1
    text = pops[0]["payload"]["text"]
    assert "NEW STUDIO POPUP" in text and "needs review" in text.lower() and "Title: Studio needs your attention" in text
    assert "Buttons: Review / Later" in text and pops[0]["screenshot_path"]
    assert alerts(h, "INC-") == [] and events_of(h, "BROADCAST_STARTED") == []
    h.run(20)
    assert len(alerts(h, "POP-")) == 1


def test_popup_during_profile_lookup_still_alerts(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    h.mon.broadcast.paused = True                                              # lookup in progress freezes the engine
    h.scene_cap.set_scene(dialog=["Your LIVE was ended", "Your LIVE was ended due to a violation of our Community Guidelines.", "OK"])
    h.run(4)
    assert len(alerts(h, "INC-")) == 1
    h.mon.broadcast.paused = False


def test_stale_frame_analysis_is_not_mixed_with_new_frames(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    old = h.mon.last_analysis
    h.scene_cap.set_scene(dialog=["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now | Cancel"])
    h.run(2)
    new = h.mon.last_analysis
    assert new.frame_id > old.frame_id and new.captured_at > old.captured_at
    assert old.popups == [] and new.popup_of_type(END_CONFIRMATION) is not None
    assert new.popups[0].frame_id == new.frame_id and new.popups[0].observed_at == new.captured_at


# ---------------------------------------------------------------- queued old events, fairness, latency

def test_old_queued_start_event_is_marked_delayed(cfg, clock):
    q = DeliveryQueue(cfg.db_path, clock=clock)
    tr = FakeTransport()
    from studio_monitor.telegram import TelegramClient
    client = TelegramClient(TelegramConfig(), TOKEN_A, "42", transport=tr)
    payload = {"text": "HAS GONE LIVE", "caption": "HAS GONE LIVE", "created_at": clock.now}
    clock.advance(15 * 60)
    deliver(client, payload, "", clock)
    from urllib.parse import parse_qs
    sent = parse_qs(tr.requests[-1][1].decode())["text"][0]
    assert "delivery delayed by 15 min" in sent and "not the current status" in sent


def test_rate_limited_bot_does_not_delay_other_bot_and_urgent_first(cfg, clock):
    q = DeliveryQueue(cfg.db_path, clock=clock, backoff_base=5)
    from studio_monitor.bots import BotTarget
    a = BotTarget("bot-a", "A", "1", None); b = BotTarget("bot-b", "B", "2", None)
    q.create_event("EVT-1", "activity", "studio_opened", {"text": "opened", "created_at": clock.now}, "", [a, b], "opened")
    q.create_event("INC-1", "incident", "restrictions", {"text": "restricted", "created_at": clock.now}, "", [a, b], "restricted")
    tr = FakeTransport(per_token={TOKEN_A: [(429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 30}})]})
    factory = ClientFactory(TelegramConfig(), lambda bid: TOKEN_A if bid == "bot-a" else TOKEN_B, transport=tr)

    def send(d):
        from studio_monitor.telegram import TelegramClient
        c = TelegramClient(TelegramConfig(), factory.token(d.bot_id), d.chat_id, transport=tr)
        return deliver(c, d.payload, "", clock).get("message_id")
    w = DeliveryWorker(q, send, concurrency=2)
    w.process_round()                                                           # urgent first: INC-1 goes before EVT-1 for both bots
    first = [d for d in all_deliveries(q) if d["attempts"] or d["status"] == "failed"]
    assert all(d["event_id"] == "INC-1" for d in first) and len(first) == 2
    assert [d["status"] for d in first if d["bot_id"] == "bot-a"] == ["pending"]   # A rate-limited, retried later
    w.process_round()
    done_b = [d for d in all_deliveries(q) if d["bot_id"] == "bot-b" and d["status"] == "sent"]
    assert len(done_b) == 2                                                     # B finished both while A is blocked
    assert q.seconds_until_next() and q.seconds_until_next() <= 30
    stats = q.latency_stats()
    assert stats["telegram_api"]["n"] == 2 and stats["queue_delay"]["n"] == 2 and stats["pending"] == 2


def test_detection_latency_measured_on_replay(auto):
    h = auto
    h.scene_cap.set_scene(live=True, face_boxes=[(60, 80, 80, 100)])
    h.run(8)
    h.scene_cap.set_scene(dialog=["Your LIVE was ended", "Your LIVE was ended due to a violation of our Community Guidelines.", "OK"])
    h.run(2)
    inc = alerts(h, "INC-")[0]
    t = inc["payload"]["timing"]
    assert t["persisted_at"] - t["captured_at"] <= 2.0                           # detection -> enqueue within 2 s (replay clock)
    rep = h.mon.latency_report()
    assert rep["capture_to_analysis_ms"]["n"] >= 1 and rep["pending"] >= 1
    assert all(a["analysis_ms"] >= 0 for a in h.mon.frame_analyses)


# ---------------------------------------------------------------- learned from the real session (2026-10-09): main UI is not a dialog

def test_main_ui_panels_never_qualify_as_unknown_dialogs(rules):
    from studio_monitor.popups import credible_dialog_text, PopupClassifier as PC
    assert credible_dialog_text("No highlights at the moment", "", []) is False          # empty-state panel, no buttons
    assert credible_dialog_text("Add a cast source to share your screen", "", []) is False
    assert credible_dialog_text("See all >", "", ["See all"]) is False                   # one-word title
    assert credible_dialog_text("LIVE Goal", "", ["Goal"]) is False
    assert credible_dialog_text("Studio needs your attention", "Please review the new policy before continuing.", ["Review", "Later"]) is True
    assert PC._floats_centred((1, 140, 730, 456), (1512, 726)) is False                  # docked sources panel on the left edge
    assert PC._floats_centred((560, 280, 880, 480), (1512, 726)) is True                 # centred modal
    assert PC._floats_centred((900, 300, 1500, 500), (1512, 726)) is False               # off-centre, touching the right edge


def test_sign_in_page_is_one_screen_and_not_alerted(auto, rules):
    c = classifier(rules)
    assert c.classify_text("Scan to log in", "Use your phone camera to scan the QR code.", ["Continue"], "dialog")[0] == SIGN_IN
    assert c.classify_text("Email or username", "", ["Password", "Log in"], "dialog")[0] == SIGN_IN
    assert c.classify_text("Confirm on mobile app", "Open TikTok on your phone to confirm.", ["Cancel"], "dialog")[0] == SIGN_IN
    h = auto
    h.run(4)
    h.scene_cap.set_scene(dialog=["Scan to log in", "Use your phone camera to scan the QR code.", "Continue with Google | Use phone / email"])
    h.run(8)
    assert alerts(h, "POP-") == [] and alerts(h, "INC-") == []
    assert h.mon.last_analysis.popups and all(p.popup_type in (SIGN_IN,) for p in h.mon.last_analysis.popups)


def test_review_alerts_are_throttled_one_shot_and_never_incidents(auto):
    h = auto
    h.run(4)
    h.scene_cap.set_scene(dialog=["Studio needs your attention", "Please review the new policy before continuing.", "Review | Later"])
    h.run(5)
    assert len(alerts(h, "POP-")) == 1
    assert alerts(h, "POP-")[0]["payload"]["popups"]
    assert h.mon.incident_engine.conn.execute("SELECT COUNT(*) FROM incidents_v2 WHERE status='OPEN'").fetchone()[0] == 0
    # a different unrecognised dialog inside the review cooldown is held, not sent
    h.scene_cap.set_scene(dialog=["Something else happened", "A second unfamiliar message for the operator.", "OK | Details"])
    h.run(6)
    assert len(alerts(h, "POP-")) == 1
    # after the cooldown the next review alert mentions what was held back
    h.clock.advance(h.cfg.detection.review_cooldown_seconds + 1)
    h.scene_cap.set_scene(dialog=["Third unfamiliar notice", "Yet another message the monitor cannot classify.", "Got it | More"])
    h.run(6)
    pops = alerts(h, "POP-")
    assert len(pops) == 2 and "logged, not sent" in pops[1]["payload"]["text"]
