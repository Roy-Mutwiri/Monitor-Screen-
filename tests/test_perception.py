"""Automatic layout discovery and relocalization on synthetic Studio frames
(tests/studio_synth.py renders a parameterised layout and an OCR double that
returns the boxes of the drawn text). Real-frame checks live in
test_perception_real.py (private fixture, skipped when absent)."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeClock
from studio_monitor.perception.clusters import dialog_candidates, group_blocks
from studio_monitor.perception.layout import Layout, LayoutStore, discover_layout, validate_layout
from studio_monitor.perception.tracker import LayoutTracker
from studio_monitor.perception.uia import UiaElement
from studio_synth import BoxOcr, StudioScene, render


def iou(a, b) -> float:
    ax, ay, ax2, ay2 = a; bx, by, bx2, by2 = b
    ix, iy, ix2, iy2 = max(ax, bx), max(ay, by), min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix) * max(0, iy2 - iy)
    union = (ax2 - ax) * (ay2 - ay) + (bx2 - bx) * (by2 - by) - inter
    return inter / union if union else 0.0


def contains(outer, inner, slack=6) -> bool:
    return outer[0] - slack <= inner[0] and outer[1] - slack <= inner[1] and outer[2] + slack >= inner[2] and outer[3] + slack >= inner[3]


def discover(sc: StudioScene, uia=None):
    rf = render(sc)
    lay = discover_layout(rf.image, rf.boxes, uia or [], 1000.0)
    return rf, lay


# ---------------------------------------------------------------- discovery

@pytest.mark.parametrize("size,scale", [((1512, 726), 1.0), ((1920, 1080), 1.25), ((1280, 720), 0.9), ((2560, 1440), 1.5)])
def test_discovers_core_elements_across_sizes_and_dpi(size, scale):
    rf, lay = discover(StudioScene(width=size[0], height=size[1], scale=scale, live=True, preview_content="portrait"))
    assert lay.status == "detected", lay.notes
    for key in ("program_preview", "live_control", "profile_control", "left_panel", "right_panel", "status_bar", "chat_panel", "audio_meter"):
        assert lay.get(key) is not None, (key, lay.notes)
    assert iou(lay.get("live_control").box, rf.elements["live_control"]) > 0.6
    assert iou(lay.get("program_preview").box, rf.elements["program_preview"]) > 0.7
    assert contains(rf.elements["audio_meter"], lay.get("audio_meter").box, slack=8)
    assert contains(rf.elements["profile_control"], lay.get("profile_control").box, slack=6)
    assert lay.get("live_status") is not None and "LIVE" in lay.get("live_status").detail
    for el in lay.elements.values():
        assert el.source and 0 < el.confidence <= 1 and el.frame_ts == 1000.0 and el.layout_version == 1


def test_preview_excludes_thumbnails_and_chat_avatars():
    rf, lay = discover(StudioScene(thumbnails=2, chat_avatars=3, preview_content="portrait"))
    pv = lay.get("program_preview").box
    for tb in rf.elements["thumbnails"]:
        assert iou(pv, tb) == 0.0
    assert iou(pv, rf.elements["chat_panel"]) == 0.0
    assert lay.get("presenter_search").box == pv


def test_black_preview_and_no_sources_are_reported_not_invented():
    rf, lay = discover(StudioScene(preview_content="black"))
    pv = lay.get("program_preview")
    assert pv is not None and pv.confidence < 0.5 and "band only" in pv.detail
    assert any("uniform" in n for n in lay.notes)


def test_rearranged_and_missing_panels():
    rf, lay = discover(StudioScene(layout_swap=True, preview_content="portrait"))
    assert lay.status == "detected"
    studio_view = next(b for b in rf.boxes if b.text == "Studio view")
    rp = lay.get("right_panel").box
    assert rp[0] <= studio_view.cx <= rp[2]                       # the sources panel is now on the right
    rf2, lay2 = discover(StudioScene(right_panel=False, chat=False, preview_content="portrait"))
    assert lay2.get("chat_panel") is None and lay2.get("program_preview") is not None
    assert lay2.status in ("detected", "partly")


def test_dark_meter_is_not_located_and_mixer_still_is():
    rf, lay = discover(StudioScene(meter_lit=0.0))
    assert lay.get("audio_meter") is None and lay.get("mixer") is not None
    assert any("audio meter not located" in n for n in lay.notes)


def test_no_geometry_and_no_uia_fails_honestly():
    rf = render(StudioScene())
    lay = discover_layout(rf.image, [], [], 1.0, uia_note="no accessible elements")
    assert lay.status == "failed" and "no OCR geometry" in lay.notes[0]


def test_uia_named_controls_take_precedence():
    rf = render(StudioScene())
    uia = [UiaElement("ButtonControl", "Go LIVE", "btn-live", (1300, 650, 1400, 690), 3),
           UiaElement("PaneControl", "LIVE chat", "", (1200, 300, 1500, 700), 2)]
    lay = discover_layout(rf.image, rf.boxes, uia, 1.0)
    assert lay.get("live_control").source == "uia" and lay.get("live_control").box == (1300, 650, 1400, 690)
    assert lay.get("chat_panel").source == "uia"


def test_dialog_and_banner_are_grouped_spatially():
    rf, lay = discover(StudioScene(dialog=["End streaming?", "End LIVE? Share your LIVE for more viewers.", "End now | Cancel"],
                                   banner="Realtek HD Audio 2nd output not available. Open audio settings to check."))
    kinds = {t.type for t in lay.transient}
    assert "dialog" in kinds and "banner" in kinds
    dlg = next(t for t in lay.transient if t.type == "dialog")
    assert "End streaming?" in dlg.detail and "End now" in dlg.detail
    blocks = group_blocks(rf.boxes)
    cands = dialog_candidates(blocks, lay.get("preview_band").box)
    assert any("End streaming?" in c.text for c in cands)
    # isolated words elsewhere never form a dialog
    assert not any("Go LIVE" == c.text for c in cands)


def test_modal_with_its_own_live_center_and_close_circle_does_not_fool_the_profile_locator():
    # a post-LIVE summary modal repeats "LIVE Center" lower down and has a round close button at its top-right
    rf = render(StudioScene(dialog=["That's a wrap! Here is a summary of your LIVE.", "How was your LIVE experience?", "Good | Poor"]))
    from PIL import ImageDraw
    d = ImageDraw.Draw(rf.image)
    dx0, dy0, dx1, dy1 = rf.elements["dialog"]
    d.ellipse((dx1 - 30, dy0 - 40, dx1 - 6, dy0 - 16), outline=(220, 220, 220), width=2)             # "x" close circle above the modal
    from studio_monitor.perception.ocr_boxes import OcrBox
    rf.boxes.append(OcrBox("LIVE Center", dx1 - 120, dy0 - 36, 70, 14))
    lay = discover_layout(rf.image, rf.boxes, [], 1.0)
    pc = lay.get("profile_control")
    assert pc is not None and pc.box[1] < 50 and pc.confidence >= 0.7                                 # the real one on the title row
    assert lay.get("top_bar").box[3] < 60                                                              # the bar did not stretch to the modal
    pv = lay.get("program_preview")
    assert pv is not None and pv.confidence < 0.5 and "covered" in pv.detail


def test_signature_scopes_by_size_language_and_columns():
    _, a = discover(StudioScene())
    _, b = discover(StudioScene(width=1920, height=1080))
    _, c = discover(StudioScene(layout_swap=True))
    assert a.signature != b.signature and a.signature != c.signature
    _, a2 = discover(StudioScene())
    assert a.signature == a2.signature


# ---------------------------------------------------------------- store + validation

def test_layout_store_roundtrip_and_validation(tmp_path):
    rf, lay = discover(StudioScene(live=True))
    store = LayoutStore(tmp_path / "layouts.json")
    store.put(lay)
    back = LayoutStore(tmp_path / "layouts.json").get(lay.signature)
    assert back is not None and back.get("live_control").box == lay.get("live_control").box
    ok, problems = validate_layout(back, rf.image, rf.boxes)
    assert ok, problems
    # a different frame contradicts it: the button moved
    rf2 = render(StudioScene(live=True, width=1512, height=726, layout_swap=True))
    ok, problems = validate_layout(back, rf2.image, rf2.boxes)
    assert not ok and problems
    assert back.rel_box("live_control")[0] < 1.0


# ---------------------------------------------------------------- tracker / relocalization

def tracker(tmp_path, clock, ocr, **kw):
    return LayoutTracker(ocr, LayoutStore(tmp_path / "layouts.json"), uia_probe=lambda: ([], "none"), discovery_interval=60,
                         validate_interval=5, mono=clock, clock=clock, **kw)


def test_tracker_initial_discovery_resize_restart_and_periodic(tmp_path, clock):
    ocr = BoxOcr()
    events = []
    t = tracker(tmp_path, clock, ocr, on_event=events.append)
    rf = render(StudioScene(live=True)); ocr.set_frame(rf.image, rf.boxes)
    t.observe(rf.image, clock.now, True, pid=10)
    assert t.status.state == "detected" and t.status.discoveries == 1 and "initial discovery" in events[-1]
    for _ in range(10):                                   # steady frames: cheap validation only
        clock.advance(3); t.observe(rf.image, clock.now, True, pid=10)
    assert t.status.discoveries == 1 and t.layout.validations >= 1
    rf2 = render(StudioScene(live=True, width=1920, height=1080)); ocr.set_frame(rf2.image, rf2.boxes)
    clock.advance(3); t.observe(rf2.image, clock.now, True, pid=10)
    assert t.status.discoveries == 2 and "resized" in t.status.last_reason
    clock.advance(3); t.observe(rf2.image, clock.now, True, pid=11)
    assert t.status.discoveries == 3 and "restarted" in t.status.last_reason
    clock.advance(61); t.observe(rf2.image, clock.now, True, pid=11)
    assert t.status.discoveries == 4 and "periodic" in t.status.last_reason
    assert t.status.avg_ms >= 0 and t.status.elements["live_control"]["source"]


def test_tracker_rediscovers_on_panel_change_and_contradicting_anchors(tmp_path, clock):
    ocr = BoxOcr()
    t = tracker(tmp_path, clock, ocr)
    rf = render(StudioScene(live=True)); ocr.set_frame(rf.image, rf.boxes)
    t.observe(rf.image, clock.now, True, pid=1)
    # panel closes: wholesale change -> rediscovery, chat panel disappears from the layout
    rf2 = render(StudioScene(live=True, right_panel=False, chat=False)); ocr.set_frame(rf2.image, rf2.boxes)
    clock.advance(2); t.observe(rf2.image, clock.now, True, pid=1)
    assert t.status.discoveries == 2 and t.layout.get("chat_panel") is None
    # the same-size frame with the panels swapped: cheap validation finds the red button gone -> rediscovery
    rf3 = render(StudioScene(live=True, layout_swap=True)); ocr.set_frame(rf3.image, rf3.boxes)
    sig_before = t.layout.signature
    clock.advance(6); t.observe(rf3.image, clock.now, True, pid=1)      # validation finds the red button gone
    assert t.status.discoveries == 3 and t.layout.signature != sig_before and "contradict" in t.status.last_reason


def test_invalid_frames_never_trigger_discovery(tmp_path, clock):
    ocr = BoxOcr()
    t = tracker(tmp_path, clock, ocr)
    rf = render(StudioScene()); ocr.set_frame(rf.image, rf.boxes)
    t.observe(None, clock.now, False)
    t.observe(rf.image, clock.now, False)
    assert t.status.discoveries == 0 and t.layout is None


def test_cached_profile_hints_are_revalidated_against_fresh_evidence(tmp_path, clock):
    ocr = BoxOcr()
    store = LayoutStore(tmp_path / "layouts.json")
    rf = render(StudioScene(live=True, meter_lit=0.5)); ocr.set_frame(rf.image, rf.boxes)
    lay = discover_layout(rf.image, rf.boxes, [], 1.0)
    # poison the cache: a profile with the same signature but the live control somewhere else
    bad = Layout.from_dict(lay.to_dict())
    el = bad.elements["live_control"]; bad.elements["live_control"] = type(el)(el.type, (10, 10, 60, 30), 0.99, "ocr+visual", 1.0)
    store.put(bad)
    t = LayoutTracker(ocr, store, uia_probe=lambda: ([], "none"), mono=clock, clock=clock)
    # a frame where the meter is dark: discovery is "partly" -> the cache is consulted, but contradicted -> ignored
    rf2 = render(StudioScene(live=True, meter_lit=0.0)); ocr.set_frame(rf2.image, rf2.boxes)
    t.observe(rf2.image, clock.now, True)
    assert t.layout.get("live_control").box != (10, 10, 60, 30)
    assert t.status.from_cache is False


def test_degraded_ocr_without_geometry_is_flagged(tmp_path, clock):
    ocr = BoxOcr(with_geometry=False)
    t = tracker(tmp_path, clock, ocr)
    rf = render(StudioScene()); ocr.set_frame(rf.image, rf.boxes)
    t.observe(rf.image, clock.now, True)
    assert t.status.ocr_geometry is False and t.status.state == "failed"
