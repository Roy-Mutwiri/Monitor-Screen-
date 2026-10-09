"""Owner name ("Whose PC?") -> notification label: validation, persistence,
fallback, caption formatting and label preservation on queued events."""
import pytest

from conftest import all_deliveries
from test_activity import LIVE_TEXT, Harness

from studio_monitor.alerts import (format_alert, format_broadcast_started, format_health_alert,
                                   format_not_live_reminder, format_studio_closed, format_studio_opened,
                                   format_test_notification, headline)
from studio_monitor.config import AppConfig
from studio_monitor.incidents import Incident
from studio_monitor.labels import MAX_OWNER_NAME, OwnerNameError, notification_label, validate_owner_name

POPUP = "Your LIVE has been restricted for violating our Community Guidelines"
PUZZLE = "Verify to continue: drag the slider to fit the puzzle piece"


@pytest.mark.parametrize("raw,clean", [
    ("  Roy  ", "Roy"), ("", ""), ("Zoë O'Brien-Smith!", "Zoë O'Brien-Smith!"), ("李雷 & 韩梅梅", "李雷 & 韩梅梅"),
    ("x" * 60, "x" * 60),
])
def test_owner_name_accepts_unicode_punctuation_and_trims(raw, clean):
    assert validate_owner_name(raw) == clean


@pytest.mark.parametrize("bad,needle", [
    ("Roy\nTwo", "single line"), ("Roy\rTwo", "single line"), ("Roy\tTab", "single line"),
    ("Roy\x07", "control"), ("Roy‎", "control"), ("x" * 61, "60 characters"),
])
def test_owner_name_rejects_multiline_control_and_too_long(bad, needle):
    with pytest.raises(OwnerNameError, match=needle):
        validate_owner_name(bad)
    assert MAX_OWNER_NAME == 60


def test_label_and_fallback():
    assert notification_label("Roy", "DESKTOP-1") == "Roy’s Live"
    assert notification_label("", "DESKTOP-1") == "DESKTOP-1’s Live"
    assert notification_label("   ", "DESKTOP-1") == "DESKTOP-1’s Live"
    cfg = AppConfig(); cfg.machine_label = "studio-pc"
    assert cfg.notification_label == "studio-pc’s Live"
    cfg.owner_name = "Roy"
    assert cfg.notification_label == "Roy’s Live"


def test_owner_persists_and_old_settings_default_blank(tmp_path):
    p = tmp_path / "config.json"
    cfg = AppConfig(); cfg.machine_label = "pc"; cfg.owner_name = "Roy"
    cfg.save(p)
    back = AppConfig.load(p)
    assert back.owner_name == "Roy" and back.notification_label == "Roy’s Live"
    legacy = AppConfig.from_dict({"machine_label": "pc"})                 # settings written before this feature
    assert legacy.owner_name == "" and legacy.notification_label == "pc’s Live"
    bad = AppConfig.from_dict({"machine_label": "pc", "owner_name": "x\ny"})
    assert bad.owner_name == ""                                           # invalid stored value is ignored safely


def test_headline_escapes_operator_text():
    assert headline("\U0001F534", "<Roy & Co>", "HAS GONE LIVE") == "\U0001F534 <b>&lt;Roy &amp; Co&gt; — HAS GONE LIVE</b>"


def test_every_notification_type_uses_the_label():
    label = "Roy’s Live"
    inc = Incident("INC-1", "restriction_notice", "Restriction notice", "restricted <x>", "f", 1.0, 1.0, 1.0)
    puzzle = Incident("INC-2", "verification_puzzle", "Verification puzzle", "drag the slider", "f", 1.0, 1.0, 1.0,
                      manual_attention=True)
    caps = {
        "restriction": format_alert(inc, "pc", label=label)["caption"],
        "puzzle": format_alert(puzzle, "pc", label=label)["caption"],
        "opened": format_studio_opened("pc", 1.0, True, label=label)["caption"],
        "closed": format_studio_closed("pc", 1.0, None, label=label)["caption"],
        "reminder": format_not_live_reminder("pc", 1.0, 60, 3600, "EP", label=label)["caption"],
        "live": format_broadcast_started("pc", 1.0, label=label)["caption"],
        "degraded": format_health_alert("degraded", "Studio is minimized", "pc", 1.0, 0.0, 20, label=label)["caption"],
        "recovered": format_health_alert("recovered", "Studio is minimized", "pc", 1.0, 0.0, 20, label=label)["caption"],
        "test": format_test_notification(label, "Bot", "42", "pc", 1.0, True)["caption"],
    }
    for name, cap in caps.items():
        assert cap.startswith(("⚠️", "\U0001F9E9", "\U0001F7E2", "⚫", "⏰", "\U0001F534", "✅", "\U0001F9EA")), name
        assert "<b>Roy’s Live — " in cap, name
    assert "RESTRICTION DETECTED" in caps["restriction"] and "<b>Reason:</b> <i>restricted &lt;x&gt;</i>" in caps["restriction"]
    assert "VERIFICATION REQUIRED" in caps["puzzle"] and "Manual attention required." in caps["puzzle"]
    assert "GO-LIVE REMINDER" in caps["reminder"] and "confirmed not live for at least 1 hour" in caps["reminder"]
    assert "HAS GONE LIVE" in caps["live"] and "TikTok LIVE Studio is broadcasting." in caps["live"] and "Detected at:" in caps["live"]
    assert "MONITOR DEGRADED" in caps["degraded"] and "MONITOR RECOVERED" in caps["recovered"]
    assert "STUDIO OPENED" in caps["opened"] and "STUDIO CLOSED" in caps["closed"] and "TEST NOTIFICATION" in caps["test"]


def test_queued_events_keep_their_original_label(cfg, rules, clock):
    cfg.owner_name = "Roy"
    h = Harness(cfg, rules, clock)
    h.ocr.default = POPUP
    h.run(4)
    first = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"][0]
    assert "Roy’s Live — RESTRICTION DETECTED" in first["payload"]["caption"]
    assert first["payload"]["owner_label"] == "Roy’s Live"
    cfg.owner_name = "Ann"                                                # changed while the first is still queued
    h.ocr.default = PUZZLE
    clock.advance(700)
    h.run(4)
    ds = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"]
    assert len(ds) == 2
    assert "Roy’s Live" in ds[0]["payload"]["caption"] and ds[0]["status"] == "pending"
    assert "Ann’s Live — VERIFICATION REQUIRED" in ds[1]["payload"]["caption"]
    labels = {e["event_id"]: e["owner_label"] for e in h.queue.events_history(50)}
    assert labels[ds[0]["event_id"]] == "Roy’s Live" and labels[ds[1]["event_id"]] == "Ann’s Live"
    assert h.queue.history(10)[0]["owner_label"] == "Ann’s Live"


def test_broadcast_and_activity_events_carry_label(cfg, rules, clock):
    cfg.owner_name = "Zoë"
    h = Harness(cfg, rules, clock)
    h.run(60, step=10)
    h.ocr.default = LIVE_TEXT
    h.run(60, step=10)
    caps = [d["payload"]["caption"] for d in all_deliveries(h.queue)]
    assert any("Zoë’s Live — STUDIO ALREADY RUNNING" in c for c in caps)
    assert any("Zoë’s Live — HAS GONE LIVE" in c for c in caps)
    assert all(e["owner_label"] == "Zoë’s Live" for e in h.queue.events_history(50))


def test_blank_owner_uses_machine_label_in_captions(cfg, rules, clock):
    cfg.owner_name = ""
    h = Harness(cfg, rules, clock)
    h.ocr.default = POPUP
    h.run(4)
    cap = [d for d in all_deliveries(h.queue) if d["kind"] == "incident"][0]["payload"]["caption"]
    assert cap.startswith("⚠️ <b>test-pc’s Live — RESTRICTION DETECTED</b>")
