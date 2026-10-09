"""OCR word/line boxes. Windows OCR (winocr) returns per-word bounding
rectangles; other backends only return lines, in which case discovery runs
in a degraded "text without geometry" mode and says so."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from PIL import Image


@dataclass(frozen=True)
class OcrBox:
    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def offset(self, dx: int, dy: int) -> "OcrBox":
        return OcrBox(self.text, self.x + dx, self.y + dy, self.w, self.h)


def boxes_from_winocr(data: dict, scale: float = 1.0) -> list[OcrBox]:
    """Line boxes from winocr's result dict (union of its word rectangles)."""
    out: list[OcrBox] = []
    for ln in data.get("lines", []) or []:
        words = ln.get("words") or []
        rects = [w.get("bounding_rect") for w in words if w.get("bounding_rect")]
        text = ln.get("text") or " ".join(w.get("text", "") for w in words)
        if not rects or not text.strip():
            continue
        x0 = min(r["x"] for r in rects); y0 = min(r["y"] for r in rects)
        x1 = max(r["x"] + r["width"] for r in rects); y1 = max(r["y"] + r["height"] for r in rects)
        out.append(OcrBox(text.strip(), int(x0 / scale), int(y0 / scale), max(1, int((x1 - x0) / scale)), max(1, int((y1 - y0) / scale))))
    return out


def recognize_boxes(ocr: Any, image: Image.Image) -> tuple[str, list[OcrBox], bool]:
    """(text, boxes, has_geometry). Uses ``ocr.recognize_boxes`` when the backend provides it."""
    fn = getattr(ocr, "recognize_boxes", None)
    if fn is not None:
        res = fn(image)
        return res.text, list(getattr(res, "boxes", []) or []), bool(getattr(res, "boxes", None))
    res = ocr.recognize(image)
    return res.text, [], False


def boxes_in(boxes: Iterable[OcrBox], x: int, y: int, x2: int, y2: int) -> list[OcrBox]:
    return [b for b in boxes if b.cx >= x and b.cx <= x2 and b.cy >= y and b.cy <= y2]


def find_text(boxes: Iterable[OcrBox], *needles: str) -> Optional[OcrBox]:
    low = [n.lower() for n in needles]
    for b in boxes:
        t = b.text.lower()
        if any(n in t for n in low):
            return b
    return None
