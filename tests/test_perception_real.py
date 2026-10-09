"""Real-evidence checks on the development PC only: the operator's private Studio frame and dialog crop
(git-ignored) through Windows OCR and the discovery pipeline. Skipped wherever the fixtures or Windows OCR are absent."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

PRIV = Path(__file__).with_name("fixtures") / "private"
FRAME = PRIV / "studio_frame_now.png"


def ocr():
    try:
        from studio_monitor.ocr import create_backend
        return create_backend("windows", "en", 1.0)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Windows OCR unavailable: {exc}")


@pytest.mark.skipif(not FRAME.exists(), reason="private real Studio frame not present")
def test_real_frame_discovery_locates_core_elements():
    from studio_monitor.perception.layout import discover_layout, validate_layout
    img = Image.open(FRAME).convert("RGB")
    res = ocr().recognize_boxes(img)
    assert res.boxes, "Windows OCR returned no geometry"
    lay = discover_layout(img, res.boxes, [], 1.0)
    assert lay.status == "detected", lay.notes
    lc = lay.get("live_control"); assert lc and "Go LIVE" in lc.detail and lc.confidence >= 0.9
    pv = lay.get("program_preview"); assert pv and pv.box[0] > 500 and pv.box[2] < 1000 and pv.confidence >= 0.6     # the portrait preview column
    assert lay.get("audio_meter") is not None and lay.get("mixer") is not None
    assert lay.get("profile_control") is not None and lay.get("profile_control").box[1] < 40
    assert lay.get("chat_panel") is not None and lay.get("status_bar") is not None
    assert any(t.type == "banner" and "audio" in t.detail.lower() for t in lay.transient)       # the audio-device warning banner
    ok, problems = validate_layout(lay, img, res.boxes)
    assert ok, problems


@pytest.mark.skipif(not FRAME.exists(), reason="private real Studio frame not present")
def test_real_frame_uia_tree_is_empty_so_ocr_path_is_used():
    # Documented fact for this Studio build: no accessible children. The probe must not raise on a foreign hwnd either.
    from studio_monitor.perception.uia import uia_elements
    elements, note = uia_elements(0, (0, 0), budget_seconds=0.5)
    assert elements == [] and note
