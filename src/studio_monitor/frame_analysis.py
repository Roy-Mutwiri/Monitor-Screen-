"""Per-frame analysis record: everything observed on one captured frame is
tied to its frame id and capture time, popups are classified first, the
regions they obscure are marked, and only then is the broadcast state
evaluated on the *unobscured* text. Nothing from another frame is mixed in.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from .broadcast import Classification, LiveRules, LiveState
from .perception.ocr_boxes import OcrBox
from .popups import END_CONFIRMATION, POST_LIVE_SUMMARY, PopupClassifier, PopupObservation

Box = tuple[int, int, int, int]


@dataclass
class FrameAnalysis:
    frame_id: int
    captured_at: float
    captured_mono: float
    size: tuple[int, int]
    text: str = ""
    lines: list[str] = field(default_factory=list)
    boxes: list[OcrBox] = field(default_factory=list)
    has_geometry: bool = False
    popups: list[PopupObservation] = field(default_factory=list)
    obscured: list[Box] = field(default_factory=list)
    live: Optional[Classification] = None
    live_valid: bool = True                      # False -> no broadcast observation from this frame
    live_note: str = ""
    control_label: str = ""
    analysis_started_mono: float = 0.0
    analysis_done_mono: float = 0.0
    timings_ms: dict = field(default_factory=dict)

    @property
    def analysis_ms(self) -> float:
        return (self.analysis_done_mono - self.analysis_started_mono) * 1000 if self.analysis_done_mono else 0.0

    def popup_of_type(self, *types: str) -> Optional[PopupObservation]:
        return next((p for p in self.popups if p.popup_type in types), None)

    def obscures(self, box: Box, fraction: float = 0.2) -> bool:
        x, y, x2, y2 = box
        area = max(1, (x2 - x) * (y2 - y))
        for ox, oy, ox2, oy2 in self.obscured:
            ix, iy, ix2, iy2 = max(x, ox), max(y, oy), min(x2, ox2), min(y2, oy2)
            if ix2 > ix and iy2 > iy and (ix2 - ix) * (iy2 - iy) >= fraction * area:
                return True
        return False


def analyze_frame(frame: Image.Image, frame_id: int, captured_at: float, captured_mono: float, text: str, lines: list[str],
                  boxes: list[OcrBox], classifier: Optional[PopupClassifier], live_rules: LiveRules, layout=None,
                  mono=time.monotonic) -> FrameAnalysis:
    fa = FrameAnalysis(frame_id, captured_at, captured_mono, frame.size, text, list(lines), list(boxes), bool(boxes))
    fa.analysis_started_mono = mono()
    t0 = mono()
    # 1. popups first (spatial when geometry exists, line adjacency otherwise)
    if classifier is not None:
        if boxes:
            fa.popups = classifier.classify_frame(frame, boxes, frame_id, captured_at, layout)
        else:
            fa.popups = classifier.classify_lines(lines or text.splitlines(), frame_id, captured_at, frame.size)
    fa.timings_ms["popups"] = (mono() - t0) * 1000
    fa.timings_ms["reocr"] = getattr(classifier, "last_reocr_ms", 0.0) if classifier is not None else 0.0
    # 2. obscured regions = popup panels (dialogs and banners)
    fa.obscured = [p.bounding_box for p in fa.popups if p.bounding_box != (0, 0, *frame.size)]
    # 3. broadcast evidence from unobscured, origin-aware text
    t0 = mono()
    exclude: list[Box] = list(fa.obscured)
    control_label = ""
    if layout is not None:
        for key in ("chat_panel", "top_bar", "right_panel", "left_panel", "status_bar"):
            el = layout.get(key)
            if el is not None:
                exclude.append(el.box)
        lc = layout.get("live_control")
        if lc is not None and not fa.obscures(lc.box, 0.5):
            # the control's label is read from THIS frame (never the cached layout detail, which may be stale)
            x, y, x2, y2 = lc.box
            inside = [b.text for b in boxes if x - 4 <= b.cx <= x2 + 4 and y - 4 <= b.cy <= y2 + 4]
            control_label = " ".join(inside).strip()
    end_dialog = fa.popup_of_type(END_CONFIRMATION)
    summary = fa.popup_of_type(POST_LIVE_SUMMARY)
    if end_dialog is not None and not boxes:
        # without geometry the dialog's own lines are removed from the evidence text before classification
        from .detection.rules import normalize_text as _norm
        dialog_lines = {_norm(l) for l in end_dialog.evidence}
        remaining = [ln for ln in (lines or text.splitlines()) if _norm(ln) and _norm(ln) not in dialog_lines]
        fa.live = live_rules.classify_frame("\n".join(remaining), None, [], "")
        fa.live_note = "end-stream dialog visible; its lines were excluded from broadcast evidence (no OCR geometry)"
    else:
        fa.live = live_rules.classify_frame(text, boxes if boxes else None, exclude, control_label)
        if summary is not None and fa.live.state == LiveState.LIVE:
            fa.live = Classification(LiveState.UNKNOWN, fa.live.live_score, fa.live.not_live_score, fa.live.evidence,
                                     "post-LIVE summary visible; LIVE evidence not trusted")
            fa.live_note = fa.live.reason
    fa.control_label = control_label
    fa.timings_ms["live"] = (mono() - t0) * 1000
    fa.analysis_done_mono = mono()
    return fa
