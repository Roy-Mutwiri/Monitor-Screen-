import os
import time

import pytest
from PIL import Image

from studio_monitor import SOURCE_LABEL
from studio_monitor.alerts import MANUAL_ATTENTION, format_alert
from studio_monitor.config import AppConfig, PrivacyConfig
from studio_monitor.incidents import Incident
from studio_monitor.privacy import bounded_text, mask_secret, purge_old_screenshots, redact
from studio_monitor.regions import Region


def test_config_roundtrip(tmp_path, cfg):
    cfg.regions = [Region("a", 0.1, 0.2, 0.3, 0.4), Region("b", 0, 0, 1, 1, "redact")]
    p = tmp_path / "config.json"
    cfg.save(p)
    back = AppConfig.load(p)
    assert back.target.exe_name == "TikTok LIVE Studio.exe"
    assert back.regions == cfg.regions
    assert back.telegram.chat_id == "42" and back.machine_label == "test-pc"


def test_config_ignores_unknown_keys_and_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text('{"telegram": {"bot_token": "x", "bogus": 1}, "detection": {"nope": true}}')
    c = AppConfig.load(p)
    monkeypatch.setenv("STUDIO_MONITOR_TELEGRAM_TOKEN", "envtoken")
    monkeypatch.setenv("STUDIO_MONITOR_MACHINE_LABEL", "studio-pc-1")
    c.apply_env_overrides()
    assert c.telegram.bot_token == "envtoken" and c.machine_label == "studio-pc-1"


def test_region_follows_resize():
    r = Region("n", 0.5, 0.5, 0.25, 0.25)
    assert r.to_box(1000, 800) == (500, 400, 750, 600)
    assert r.to_box(2000, 1600) == (1000, 800, 1500, 1200)


def test_region_from_pixels_and_validation():
    r = Region.from_pixels("x", (600, 50, 100, 350), (1000, 1000))  # reversed corners ok
    assert (r.x, r.y, r.w, r.h) == (0.1, 0.05, 0.5, 0.3)
    with pytest.raises(ValueError):
        Region("bad", 0.2, 0.2, 0, 0.1)
    with pytest.raises(ValueError):
        Region("bad", 1.5, 0, 0.1, 0.1)


def test_redact_only_redact_regions():
    img = Image.new("RGB", (100, 100), (200, 200, 200))
    out = redact(img, [Region("d", 0, 0, 0.5, 1, "detect"), Region("r", 0.5, 0, 0.5, 1, "redact")])
    assert out.getpixel((10, 10)) == (200, 200, 200) and out.getpixel((90, 90)) == (0, 0, 0)
    assert redact(img, []) is img


def test_bounded_text_and_mask():
    assert bounded_text("a  b\n c", 100) == "a b c"
    assert bounded_text("x" * 50, 10).endswith("…") and len(bounded_text("x" * 50, 10)) == 10
    assert mask_secret("123456:ABCDEF") == "*********CDEF"


def test_purge_old_screenshots(tmp_path):
    old = tmp_path / "old.png"; old.write_bytes(b"x")
    new = tmp_path / "new.png"; new.write_bytes(b"x")
    now = time.time()
    os.utime(old, (now - 10 * 86400, now - 10 * 86400))
    assert purge_old_screenshots(tmp_path, PrivacyConfig(screenshot_retention_days=7), now) == 1
    assert new.exists() and not old.exists()
    assert purge_old_screenshots(tmp_path, PrivacyConfig(screenshot_retention_days=0), now) == 0


def _incident(**kw):
    base = dict(incident_id="INC-20261009-120000-AB12", category="restriction_notice", label="Restriction notice",
                text="Your LIVE has been <restricted>", fingerprint="f", first_seen=1.0, last_seen=1.0,
                last_alerted=1.0, window_title="TikTok LIVE Studio")
    base.update(kw)
    return Incident(**base)


def test_alert_contains_required_fields_and_escapes():
    a = format_alert(_incident(), "studio-pc", screenshot_attached=True)
    cap = a["caption"]
    assert cap.startswith("⛔ <b>TikTok LIVE Studio</b>")
    assert f"<b>Source:</b> {SOURCE_LABEL}" in cap
    assert "<b>Category:</b> restriction_notice" in cap
    assert "&lt;restricted&gt;" in cap
    assert "<b>Machine:</b> studio-pc" in cap
    assert "<code>INC-20261009-120000-AB12</code>" in cap
    assert "<b>Time:</b> " in cap and 'main window "TikTok LIVE Studio"' in cap
    assert len(cap) <= 1024


def test_alert_manual_attention_for_puzzle():
    a = format_alert(_incident(category="verification_puzzle", label="Verification puzzle", manual_attention=True,
                               is_dialog=True, window_title="Verify"), "pc")
    assert a["caption"].startswith(f"❗ <b>{MANUAL_ATTENTION}</b>")
    assert 'separate dialog "Verify"' in a["caption"]


def test_alert_text_bounded():
    a = format_alert(_incident(text="word " * 500), "pc", max_text=100)
    assert "…" in a["caption"] and len(a["caption"]) <= 1024
