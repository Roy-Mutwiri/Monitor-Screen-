"""Layout discovery: structured observations of Studio's UI elements from
accessibility names (when present), OCR word boxes and visual anchors.

Element types:
  program_preview   the main broadcast/program preview (content rectangle)
  presenter_search  where faces are searched (== program_preview content)
  live_status       LIVE badge / broadcast timer text
  live_control      Go LIVE / End LIVE button (observation only)
  profile_control   profile/account control (avatar) in the top bar
  mixer             audio control row (sliders, meters)
  audio_meter       an individual level meter
  chat_panel        LIVE chat panel (sensitive: viewer names)
  left_panel / right_panel / top_bar / control_bar / status_bar
  dialog / banner   modal or banner blocks found this frame (transient)
Each element records bbox, confidence, evidence source, frame timestamp, the
layout version and validity. Relative coordinates are cached only after an
evidence-based discovery (LayoutStore) and are revalidated before use.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PIL import Image

from .anchors import LIVE_TIMER_RE, content_rect, find_avatar_circle, find_bars, find_colored_button, find_filled_disc, grow_panel, refine_band
from .clusters import TextBlock, dialog_candidates, group_blocks
from .ocr_boxes import OcrBox, boxes_in, find_text
from .uia import UiaElement

LAYOUT_VERSION = 1
Box = tuple[int, int, int, int]

# anchor vocabularies (lower-case, substrings); extended per language in rules/layout_anchors.json when present
ANCHORS = {
    "left_panel": ["studio view", "add source", "scenes", "sources", "tools", "general"],
    "right_panel": ["live chat", "live performance", "live data", "comments", "add comments"],
    "live_control": ["go live", "end live", "start live"],
    "status_bar": ["cpu:", "memory:", "upload:", "fps:", "frame drops"],
    "top_bar": ["live center", "tiktok live studio"],
    "camera": ["add camera source"],
}


@dataclass
class LayoutElement:
    type: str
    box: Box                                  # pixels in the frame used for discovery
    confidence: float
    source: str                               # "uia" | "ocr" | "visual" | "ocr+visual" | "cache" | "omniparser"
    frame_ts: float
    layout_version: int = LAYOUT_VERSION
    valid: bool = True
    detail: str = ""

    def rel(self, size: tuple[int, int]) -> tuple[float, float, float, float]:
        w, h = size
        x, y, x2, y2 = self.box
        return (x / w, y / h, (x2 - x) / w, (y2 - y) / h)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Layout:
    size: tuple[int, int]
    elements: dict[str, LayoutElement] = field(default_factory=dict)
    transient: list[LayoutElement] = field(default_factory=list)     # dialogs / banners seen this frame
    signature: str = ""
    language: str = "en"
    studio_version: str = ""
    discovered_utc: str = ""
    frame_ts: float = 0.0
    notes: list[str] = field(default_factory=list)
    status: str = "locating"                                         # detected | partly | locating | failed
    uia_note: str = ""
    validations: int = 0

    def get(self, type_: str) -> Optional[LayoutElement]:
        el = self.elements.get(type_)
        return el if el is not None and el.valid else None

    def rel_box(self, type_: str) -> Optional[tuple[float, float, float, float]]:
        el = self.get(type_)
        return el.rel(self.size) if el else None

    def to_dict(self) -> dict:
        return {"size": list(self.size), "elements": {k: v.to_dict() for k, v in self.elements.items()}, "signature": self.signature,
                "language": self.language, "studio_version": self.studio_version, "discovered_utc": self.discovered_utc,
                "frame_ts": self.frame_ts, "notes": self.notes, "status": self.status, "uia_note": self.uia_note,
                "validations": self.validations, "layout_version": LAYOUT_VERSION}

    @classmethod
    def from_dict(cls, d: dict) -> "Layout":
        lay = cls(tuple(d.get("size", (0, 0))), signature=d.get("signature", ""), language=d.get("language", "en"),
                  studio_version=d.get("studio_version", ""), discovered_utc=d.get("discovered_utc", ""), frame_ts=float(d.get("frame_ts", 0)),
                  notes=list(d.get("notes", [])), status=d.get("status", "locating"), uia_note=d.get("uia_note", ""),
                  validations=int(d.get("validations", 0)))
        for k, v in (d.get("elements") or {}).items():
            v = dict(v); v["box"] = tuple(v["box"])
            lay.elements[k] = LayoutElement(**{kk: vv for kk, vv in v.items() if kk in LayoutElement.__dataclass_fields__})
        return lay

    def scaled_to(self, size: tuple[int, int], source: str = "cache") -> "Layout":
        """Relative-coordinate transfer to another frame size (only valid for the same signature family)."""
        lay = Layout(size, signature=self.signature, language=self.language, studio_version=self.studio_version,
                     discovered_utc=self.discovered_utc, status=self.status, notes=list(self.notes))
        w0, h0 = self.size
        for k, el in self.elements.items():
            x, y, x2, y2 = el.box
            lay.elements[k] = LayoutElement(el.type, (int(x * size[0] / w0), int(y * size[1] / h0), int(x2 * size[0] / w0), int(y2 * size[1] / h0)),
                                            el.confidence * 0.9, source, el.frame_ts, el.layout_version, el.valid, el.detail)
        return lay


# ---------------------------------------------------------------- signature

def layout_signature(size: tuple[int, int], dpi_scale: float, language: str, studio_version: str, columns: tuple[int, int]) -> str:
    """Scope key for cached layouts: Studio version, language, window size bucket, DPI and the panel column split."""
    w, h = size
    key = f"{studio_version}|{language}|{w // 64}x{h // 64}|{round(dpi_scale, 2)}|{columns[0] // 32}-{columns[1] // 32}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- discovery

def _anchor_boxes(boxes: list[OcrBox], words: Iterable[str]) -> list[OcrBox]:
    ws = [w.lower() for w in words]
    return [b for b in boxes if any(w in b.text.lower() for w in ws)]


def _columns(boxes: list[OcrBox], size: tuple[int, int]) -> tuple[int, int, float]:
    """Left/right panel boundaries from anchor text: returns (left_edge_x, right_edge_x, confidence)."""
    w, h = size
    a = _anchor_boxes(boxes, ANCHORS["left_panel"])        # sources/tools panel
    b = _anchor_boxes(boxes, ANCHORS["right_panel"])       # LIVE data / chat panel
    if a and b:
        ca = sum(x.cx for x in a) / len(a); cb = sum(x.cx for x in b) / len(b)
        left, right = (a, b) if ca <= cb else (b, a)       # panels may be rearranged
        lx, rx, conf = max(x.x2 for x in left), min(x.x for x in right), 1.0
    elif a or b:
        g = a or b
        cg = sum(x.cx for x in g) / len(g)
        if cg < w / 2:
            lx, rx = max(x.x2 for x in g), w
        else:
            lx, rx = 0, min(x.x for x in g)
        conf = 0.5
    else:
        return 0, w, 0.0
    # sanity: the centre band must be a reasonable width
    if rx - lx < w * 0.25:
        return 0, w, 0.0
    return lx, rx, conf


def discover_layout(frame: Image.Image, boxes: list[OcrBox], uia: list[UiaElement], frame_ts: float, *,
                    dpi_scale: float = 1.0, studio_version: str = "", language: str = "en", uia_note: str = "",
                    anchors: Optional[dict] = None, prior: Optional[Layout] = None) -> Layout:
    """Evidence-based discovery on one frame. Never raises; missing parts are simply absent with a note."""
    size = frame.size
    w, h = size
    lay = Layout(size, frame_ts=frame_ts, language=language, studio_version=studio_version, uia_note=uia_note)
    vocab = {**ANCHORS, **(anchors or {})}
    notes = lay.notes
    if not boxes and not uia:
        lay.status = "failed"
        notes.append("no OCR geometry and no accessible elements")
        return lay

    def put(type_: str, box: Box, conf: float, source: str, detail: str = "") -> None:
        x, y, x2, y2 = box
        box = (max(0, int(x)), max(0, int(y)), min(w, int(x2)), min(h, int(y2)))
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            return
        cur = lay.elements.get(type_)
        if cur is not None and cur.source == "uia" and source != "uia":
            return                                          # accessibility names are authoritative
        if cur is None or conf > cur.confidence:
            lay.elements[type_] = LayoutElement(type_, box, round(min(1.0, conf), 2), source, frame_ts, detail=detail)

    # -- accessibility first: named controls are authoritative
    for el in uia:
        n = el.name.lower()
        if not n:
            continue
        if any(wd in n for wd in vocab["live_control"]):
            put("live_control", el.box, 0.99, "uia", el.name)
        if "live chat" in n or "chat" == n:
            put("chat_panel", el.box, 0.9, "uia", el.name)
        if "profile" in n or "account" in n or "avatar" in n:
            put("profile_control", el.box, 0.9, "uia", el.name)

    # -- columns from OCR anchors
    lx, rx, col_conf = _columns(boxes, size)
    top_boxes = [b for b in boxes if b.y2 < h * 0.12]
    title_row = _anchor_boxes(top_boxes, vocab["top_bar"])
    if title_row:
        top_y = min(b.y for b in title_row)
        title_row = [b for b in title_row if b.y <= top_y + max(8, b.h)]        # the topmost row only
    top_bar_bottom = int(max((b.y2 for b in title_row), default=int(h * 0.06))) + 6
    top_boxes = [b for b in top_boxes if b.y2 <= top_bar_bottom]
    status = _anchor_boxes(boxes, vocab["status_bar"])
    status_top = int(min((b.y for b in status), default=h)) - 4 if status else h
    if status:
        put("status_bar", (0, status_top, w, h), 0.6 + 0.1 * min(4, len(status)), "ocr", ", ".join(b.text for b in status[:4]))
    if col_conf:
        if lx > 0:
            put("left_panel", (0, top_bar_bottom, lx, status_top), 0.5 + 0.4 * col_conf, "ocr")
        if rx < w:
            put("right_panel", (rx, top_bar_bottom, w, status_top), 0.5 + 0.4 * col_conf, "ocr")
    put("top_bar", (0, 0, w, top_bar_bottom), 0.6 if top_boxes else 0.3, "ocr" if top_boxes else "visual")

    # -- Go LIVE / End LIVE control (OCR text + coloured button blob)
    lc = _anchor_boxes(boxes, vocab["live_control"])
    control_bar_top = status_top
    if lc:
        tb = max(lc, key=lambda b: b.y)                       # the real control is the lowest one (chip in the top bar reads "Go LIVE!" too)
        bbox, bconf = find_colored_button(frame, tb, "red")
        if bbox:
            put("live_control", bbox, 0.75 + 0.25 * bconf, "ocr+visual", tb.text)
        else:
            put("live_control", (tb.x - 8, tb.y - 6, tb.x2 + 8, tb.y2 + 6), 0.55, "ocr", tb.text)
        el = lay.elements["live_control"]
        control_bar_top = el.box[1] - 10
        put("control_bar", (lx, control_bar_top, rx, el.box[3] + 10), el.confidence * 0.9, el.source)
        # mixer: sliders / meters in the control row left of the button
        bars = find_bars(frame, (lx, el.box[1] - 4, el.box[0] - 4, el.box[3] + 4))
        meters = [b for b in bars if b[1] == "meter"]
        tracks = [b for b in bars if b[1] == "track"]
        if bars:
            x0 = min(b[0][0] for b in bars); x1 = max(b[0][2] for b in bars)
            put("mixer", (x0 - 24, el.box[1], x1 + 8, el.box[3]), 0.5 + 0.1 * min(4, len(bars)), "visual", f"{len(tracks)} tracks, {len(meters)} meters")
        if meters:
            mb = max(meters, key=lambda b: b[0][2] - b[0][0])
            put("audio_meter", (mb[0][0], mb[0][1] - 2, mb[0][2], mb[0][3] + 2), mb[2], "visual", "green level segment")
        elif tracks:
            notes.append("mixer sliders found but no lit level segment: audio meter not located")

    # -- LIVE status badge / timer
    for b in boxes:
        m = LIVE_TIMER_RE.search(b.text)
        if m and ("live" in b.text.lower() or b.y < h * 0.5) and b.y < control_bar_top:
            put("live_status", (b.x - 4, b.y - 4, b.x2 + 4, b.y2 + 4), 0.7 if "live" in b.text.lower() else 0.5, "ocr", b.text)
            break

    # -- program preview: dark/video content between the panels, under the top bar, above the control bar
    band = (lx + 4, top_bar_bottom, rx - 4, control_bar_top)
    if band[2] - band[0] > w * 0.2 and band[3] - band[1] > h * 0.2:
        band, _canvas = refine_band(frame, band)
        put("preview_band", band, 0.4 + 0.5 * col_conf, "ocr+visual")
        cbox, cconf, cnote = content_rect(frame, band)
        if cbox:
            # must be a large central region, not a thumbnail strip: at least 25% of the band height and width > 12% of frame
            if (cbox[3] - cbox[1]) >= 0.25 * (band[3] - band[1]) and (cbox[2] - cbox[0]) >= 0.12 * w:
                put("program_preview", cbox, cconf * (0.7 + 0.3 * col_conf), "visual", cnote)
            else:
                notes.append("content in the preview band is too small to be the program preview")
        else:
            notes.append(cnote)
            put("program_preview", band, 0.35, "visual", "band only; no content detected")
    else:
        notes.append("could not establish the preview band between the side panels")
    pv = lay.get("program_preview")
    if pv is not None:
        put("presenter_search", pv.box, pv.confidence, pv.source, "faces are searched only here")

    # -- chat panel (sensitive)
    chat = find_text(boxes, "live chat")
    if chat is not None:
        put("chat_panel", (max(rx, chat.x - 16), chat.y - 8, w, status_top), 0.7, "ocr", chat.text)

    # -- profile / account control: circle in the top bar, right of "LIVE Center", left of the window buttons
    lc_text = find_text(top_boxes, "live center")
    px0 = (lc_text.x2 + 8) if lc_text else int(w * 0.75)
    row_y0, row_y1 = (lc_text.y - 10, lc_text.y2 + 10) if lc_text else (2, top_bar_bottom)
    pband = (px0, max(0, row_y0), w - 120, min(top_bar_bottom, row_y1))
    pbox, pconf = find_filled_disc(frame, pband)                              # a filled avatar disc beats stroke icons
    pdetail = "filled disc (avatar) on the title row"
    if not pbox:
        pbox, pconf = find_avatar_circle(frame, pband)
        pconf = min(pconf, 0.5)                                                # ring/stroke circles (bell, gear) stay below the click threshold
        pdetail = "circular outline in the top bar (low confidence)"
    if pbox:
        cy = (pbox[1] + pbox[3]) / 2
        if lc_text and abs(cy - lc_text.cy) > max(8, lc_text.h):
            pconf *= 0.5                                                       # not on the title row: doubtful
        put("profile_control", pbox, pconf * (1.0 if lc_text else 0.8), "visual", pdetail)
    else:
        notes.append("profile control not located (no circular control found in the top bar)")

    # -- dialogs / banners (transient, spatially grouped)
    blocks = group_blocks(boxes)
    band_all = (lx, top_bar_bottom, rx, status_top)
    for blk in dialog_candidates(blocks, band_all):
        kind = "banner" if (blk.y2 - blk.y) < h * 0.2 and blk.y < h * 0.3 else "dialog"   # a top notice (audio/source warning) with its icon row
        panel = grow_panel(frame, (blk.x - 4, blk.y - 4, blk.x2 + 4, blk.y2 + 4))
        if (panel[2] - panel[0]) > w * 0.9 or (panel[3] - panel[1]) > h * 0.9:
            panel = (blk.x - 12, blk.y - 10, blk.x2 + 12, blk.y2 + 10)         # grew into the canvas: keep the text box
        lay.transient.append(LayoutElement(kind, panel, 0.6 + 0.1 * min(3, len(blk.buttons)), "ocr+visual", frame_ts, detail=blk.text[:200]))

    pv = lay.get("program_preview")
    if pv is not None and lay.transient:
        px, py, px2, py2 = pv.box
        area = max(1, (px2 - px) * (py2 - py))
        for t in lay.transient:
            ix, iy, ix2, iy2 = max(px, t.box[0]), max(py, t.box[1]), min(px2, t.box[2]), min(py2, t.box[3])
            if t.type == "dialog" and (ix2 - ix) > 0 and (iy2 - iy) > 0 and (ix2 - ix) * (iy2 - iy) > 0.1 * area:
                pv.confidence = round(pv.confidence * 0.5, 2)
                pv.detail += f"; partly covered by a {t.type}"
                ps = lay.get("presenter_search")
                if ps is not None:
                    ps.confidence, ps.detail = pv.confidence, ps.detail + f"; covered by a {t.type}: presenter not evaluated"
                notes.append(f"program preview partly covered by a {t.type}; presenter evaluation paused until it closes")
                break

    # -- status
    core = ("program_preview", "live_control")
    found = sum(1 for t in core if lay.get(t))
    lay.status = "detected" if found == len(core) and (lay.get("left_panel") or lay.get("right_panel")) else ("partly" if found else "failed")
    lay.signature = layout_signature(size, dpi_scale, language, studio_version, (lx, rx))
    lay.discovered_utc = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(frame_ts if frame_ts > 1e9 else time.time()))
    return lay


# ---------------------------------------------------------------- validation (cheap)

def validate_layout(lay: Layout, frame: Image.Image, boxes: Optional[list[OcrBox]] = None) -> tuple[bool, list[str]]:
    """Cheap checks that the cached/last layout still fits the frame: size, the coloured live control still at its
    box, the preview band still non-uniform where content was. Any OCR anchors given are compared too."""
    problems: list[str] = []
    if frame.size != lay.size:
        return False, [f"frame size changed {lay.size} -> {frame.size}"]
    lc = lay.get("live_control")
    if lc is not None and lc.source != "uia":
        from .anchors import _np, saturated_mask
        x, y, x2, y2 = lc.box
        sub = _np(frame)[y:y2, x:x2]
        if sub.size and float(saturated_mask(sub, "red").mean()) < 0.15:
            problems.append("live control no longer red at its cached position")
    pv = lay.get("program_preview")
    if pv is not None and pv.detail.startswith("content"):
        x, y, x2, y2 = pv.box
        import numpy as np
        sub = np.asarray(frame.convert("L").crop((x, y, x2, y2)), dtype=np.int16)
        if sub.size and float(sub.std()) < 2.0:
            problems.append("program preview area is now uniform")
    if boxes:
        lp = lay.get("left_panel")
        if lp is not None and not any(b.cx < lp.box[2] for b in boxes if any(a in b.text.lower() for a in ANCHORS["left_panel"])):
            problems.append("left panel anchors moved")
    return not problems, problems


# ---------------------------------------------------------------- store

class LayoutStore:
    """Versioned local layout profiles keyed by signature (JSON in the data dir)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict = {"version": LAYOUT_VERSION, "profiles": {}}
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("version") == LAYOUT_VERSION:
                    self._data = d
        except (OSError, ValueError):
            pass

    def get(self, signature: str) -> Optional[Layout]:
        d = self._data["profiles"].get(signature)
        return Layout.from_dict(d) if d else None

    def put(self, lay: Layout) -> None:
        if not lay.signature or lay.status == "failed":
            return
        d = lay.to_dict()
        d["elements"] = {k: v for k, v in d["elements"].items() if v["source"] != "cache"}
        self._data["profiles"][lay.signature] = d
        self._data["profiles"] = dict(list(self._data["profiles"].items())[-20:])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def count(self) -> int:
        return len(self._data["profiles"])

    def clear(self) -> None:
        self._data["profiles"] = {}
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
