"""End-to-end loop with fakes: capture -> OCR -> rules -> dedup -> queue."""
from pathlib import Path

from conftest import FakeCapturer, FakeOcr, FakeWindowSystem, make_window

from studio_monitor import SOURCE_LABEL
from studio_monitor.monitor import Monitor
from studio_monitor.queue import DeliveryQueue
from studio_monitor.regions import Region
from studio_monitor.tracker import Status
from studio_monitor.win32.windows import Rect

MAIN_SIZE = (1280, 720)
DIALOG_SIZE = (500, 300)


def build(cfg, rules, clock, sender=None):
    sys_ = FakeWindowSystem()
    sys_.add(make_window())
    cap = FakeCapturer()
    ocr = FakeOcr()
    queue = DeliveryQueue(cfg.db_path, clock=clock)
    events, statuses = [], []
    mon = Monitor(cfg, sys_, cap, ocr, rules, queue, sender, clock,
                  on_event=events.append, on_status=statuses.append)
    return mon, sys_, cap, ocr, queue, events, statuses


def test_restriction_in_main_window_queues_alert_with_screenshot(cfg, rules, clock):
    mon, sys_, cap, ocr, queue, events, statuses = build(cfg, rules, clock)
    ocr.texts[MAIN_SIZE] = "Your LIVE has been restricted for violating our Community Guidelines"
    dets = mon.tick()
    assert len(dets) == 1 and dets[0].category == "restriction_notice"
    item = queue.next_due()
    assert item is not None
    assert SOURCE_LABEL in item.payload["caption"]
    for field in ("Category:", "Detected text:", "Time:", "Machine:</b> test-pc", "Incident ID:", "main window"):
        assert field in item.payload["caption"]
    assert Path(item.screenshot_path).exists() and item.screenshot_path.endswith(f"{item.incident_id}.png")
    assert statuses[-1].status == Status.RUNNING
    inc = queue.recent_incidents()[0]
    assert inc["category"] == "restriction_notice" and inc["detected_text"].startswith("Your LIVE")


def test_duplicate_popup_not_requeued(cfg, rules, clock):
    mon, *_, queue, events, statuses = build(cfg, rules, clock)
    ocr = mon.detector.ocr
    ocr.texts[MAIN_SIZE] = "Your account has been suspended"
    for _ in range(5):
        mon.tick()
        clock.advance(2)
    assert queue.counts()["pending"] == 1


def test_verification_puzzle_in_separate_dialog(cfg, rules, clock):
    mon, sys_, cap, ocr, queue, events, _ = build(cfg, rules, clock)
    sys_.add(make_window(hwnd=0x7007, title="Verify", pid=4242, rect=Rect(400, 300, 900, 600)))
    ocr.texts[DIALOG_SIZE] = "Verify to continue: drag the slider to fit the puzzle piece"
    dets = mon.tick()
    assert dets and dets[0].is_dialog and dets[0].category == "verification_puzzle"
    item = queue.next_due()
    assert "Manual attention required" in item.payload["caption"]
    assert 'separate dialog "Verify"' in item.payload["caption"]
    assert 0x7007 in cap.calls  # the dialog itself was captured, not just the main window


def test_regions_limit_ocr_to_crops(cfg, rules, clock):
    cfg.regions = [Region("notice area", 0.25, 0.25, 0.5, 0.5), Region("chat", 0.8, 0.0, 0.2, 1.0, "redact")]
    mon, *_, ocr, queue, events, _ = build(cfg, rules, clock)
    ocr.texts[(640, 360)] = "Your LIVE has ended. We ended your LIVE."
    ocr.default = "Your LIVE has been restricted"  # would match if whole window were scanned
    dets = mon.tick()
    assert len(dets) == 1 and dets[0].category == "live_interruption" and dets[0].region.name == "notice area"
    assert ocr.calls == 1


def test_redaction_applied_before_save(cfg, rules, clock):
    from PIL import Image
    cfg.regions = [Region("chat", 0.5, 0.0, 0.5, 1.0, "redact")]
    mon, *_, ocr, queue, events, _ = build(cfg, rules, clock)
    ocr.default = "Your LIVE has been restricted"
    mon.tick()
    shot = Image.open(queue.next_due().screenshot_path)
    assert shot.getpixel((shot.width - 1, 0)) == (0, 0, 0) and shot.getpixel((0, 0)) == (40, 40, 40)


def test_privacy_no_screenshot_attachment(cfg, rules, clock):
    cfg.privacy.send_screenshots = False
    cfg.privacy.store_detected_text = False
    mon, *_, ocr, queue, events, _ = build(cfg, rules, clock)
    ocr.default = "Your LIVE has been restricted"
    mon.tick()
    item = queue.next_due()
    assert item.screenshot_path == "" and "not attached" in item.payload["text"]
    assert queue.recent_incidents()[0]["detected_text"] == ""


def test_lost_then_rediscovered_window(cfg, rules, clock):
    mon, sys_, cap, ocr, queue, events, statuses = build(cfg, rules, clock)
    saved = []
    mon._on_identity_change = saved.append
    mon.tick()
    sys_.remove(0x1001); sys_.alive.clear()
    mon.tick()
    assert statuses[-1].status == Status.LOST and "unavailable" in statuses[-1].reason
    sys_.add(make_window(hwnd=0x4444, pid=9191))
    ocr.default = "Your LIVE has been restricted"
    mon.tick()
    assert statuses[-1].status == Status.RUNNING and saved[-1].hwnd == 0x4444
    assert cfg.target.hwnd == 0x4444
    assert queue.counts()["pending"] == 1


def test_degraded_when_minimized_skips_ocr(cfg, rules, clock):
    mon, sys_, cap, ocr, queue, events, statuses = build(cfg, rules, clock)
    sys_.windows[0x1001] = make_window(minimized=True)
    ocr.default = "Your LIVE has been restricted"
    assert mon.tick() == []
    assert statuses[-1].status == Status.DEGRADED and ocr.calls == 0


def test_status_change_notification_optional(cfg, rules, clock):
    cfg.telegram.notify_status_changes = True
    mon, sys_, *_, queue, events, statuses = build(cfg, rules, clock)
    mon.tick()
    sys_.remove(0x1001); sys_.alive.clear()
    mon.tick()
    texts = []
    while (item := queue.next_due()) is not None:
        assert item.incident_id == "STATUS"
        texts.append(item.payload["text"])
        queue.mark_sent(item.id)
    assert any("RUNNING" in t for t in texts) and any("LOST" in t for t in texts)
