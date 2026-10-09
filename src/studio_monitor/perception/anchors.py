"""Visual anchor finders (numpy/PIL, optional cv2). Each returns a box plus a
confidence derived from how well the evidence matched; callers combine
them with OCR anchors in ``layout.discover_layout``."""
from __future__ import annotations

import re
from typing import Optional

import numpy as np
from PIL import Image

from .ocr_boxes import OcrBox

Box = tuple[int, int, int, int]   # x, y, x2, y2

LIVE_TIMER_RE = re.compile(r"\b(?:LIVE\s*)?(\d{1,2}:\d{2}(?::\d{2})?)\b", re.I)
GO_LIVE_WORDS = ("go live", "end live", "start live", "stop live")


def _np(frame: Image.Image) -> np.ndarray:
    return np.asarray(frame.convert("RGB"), dtype=np.int16)


def saturated_mask(arr: np.ndarray, color: str) -> np.ndarray:
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    if color == "red":
        return (r > 170) & (g < 110) & (b < 140) & (r - g > 80)
    if color == "green":
        return (g > 120) & (g - r > 50) & (g - b > 40)
    raise ValueError(color)


def blob_around(mask: np.ndarray, cx: int, cy: int, max_w: int = 400, max_h: int = 120) -> Optional[Box]:
    """Bounding box of the connected run of mask pixels around (cx, cy), grown row/column-wise."""
    h, w = mask.shape
    cx, cy = int(min(max(cx, 0), w - 1)), int(min(max(cy, 0), h - 1))
    if not mask[cy, cx]:
        # search a small neighbourhood for the nearest mask pixel
        ys, xs = np.nonzero(mask[max(0, cy - 12): cy + 13, max(0, cx - 12): cx + 13])
        if len(xs) == 0:
            return None
        cy, cx = max(0, cy - 12) + int(ys[0]), max(0, cx - 12) + int(xs[0])
    x0 = x1 = cx; y0 = y1 = cy
    row = mask[cy]
    while x0 > 0 and row[x0 - 1] and cx - x0 < max_w: x0 -= 1
    while x1 < w - 1 and row[x1 + 1] and x1 - cx < max_w: x1 += 1
    col = mask[:, cx]
    while y0 > 0 and col[y0 - 1] and cy - y0 < max_h: y0 -= 1
    while y1 < h - 1 and col[y1 + 1] and y1 - cy < max_h: y1 += 1
    return x0, y0, x1 + 1, y1 + 1


def find_colored_button(frame: Image.Image, text_box: OcrBox, color: str = "red") -> tuple[Optional[Box], float]:
    """A solid coloured button surrounding a text box (e.g. the red Go LIVE / End LIVE / End now button)."""
    arr = _np(frame)
    mask = saturated_mask(arr, color)
    # probe just left of the text (button padding), the text itself is white on red
    probes = [(text_box.x - 6, int(text_box.cy)), (text_box.x2 + 6, int(text_box.cy)), (int(text_box.cx), text_box.y - 4), (int(text_box.cx), text_box.y2 + 4)]
    for px, py in probes:
        if 0 <= px < arr.shape[1] and 0 <= py < arr.shape[0] and mask[py, px]:
            box = blob_around(mask, px, py)
            if box and box[2] - box[0] >= text_box.w and box[3] - box[1] >= text_box.h:
                fill = float(mask[box[1]:box[3], box[0]:box[2]].mean())
                return box, min(1.0, 0.6 + 0.4 * fill)
    return None, 0.0


def refine_band(frame: Image.Image, band: Box, tol: int = 5, run: int = 10) -> tuple[Box, int]:
    """Shrink the band to the canvas: starting at the OCR-derived panel edges, move inward until ``run`` consecutive
    columns have the canvas colour (the most common luminance of the band). Returns (band, canvas_luminance)."""
    x, y, x2, y2 = band
    region = np.asarray(frame.convert("L").crop((x, y, x2, y2)), dtype=np.int16)
    if region.size == 0:
        return band, 0
    canvas = int(np.bincount(region.ravel(), minlength=256).argmax())
    colmed = np.median(region, axis=0)
    is_canvas = np.abs(colmed - canvas) <= tol
    w = len(colmed)
    nx = 0
    while nx < w - run and not is_canvas[nx:nx + run].all():
        nx += 1
    nx2 = w
    while nx2 > run and not is_canvas[nx2 - run:nx2].all():
        nx2 -= 1
    if nx2 - nx < w * 0.3:
        return band, canvas
    return (x + nx, y, x + nx2, y2), canvas


def content_rect(frame: Image.Image, band: Box, tol: int = 6, density: float = 0.3, block: int = 8) -> tuple[Optional[Box], float, str]:
    """The program preview inside the canvas band: the largest contiguous run of columns, then rows, whose pixels
    differ from the canvas colour (video content *or* the black letterbox container both differ from the canvas).
    Banners/buttons are too short to pass the density test. Returns (box, confidence, note)."""
    band, canvas = refine_band(frame, band)
    x, y, x2, y2 = band
    region = np.asarray(frame.convert("L").crop((x, y, x2, y2)), dtype=np.float32)
    h, w = region.shape
    if h < 16 or w < 16:
        return None, 0.0, "band too small"
    non_canvas = np.abs(region - canvas) > tol
    cols = non_canvas.mean(axis=0) > density
    best, cur = (0, 0), None
    for i, c in enumerate(list(cols) + [False]):
        if c and cur is None:
            cur = i
        elif not c and cur is not None:
            if i - cur > best[1] - best[0]:
                best = (cur, i)
            cur = None
    c0, c1 = best
    if c1 - c0 < 16:
        return None, 0.0, "preview band is uniform (preview black or no sources)"
    rows = np.nonzero(non_canvas[:, c0:c1].mean(axis=1) > density)[0]
    if len(rows) == 0:
        return None, 0.0, "preview band is uniform (preview black or no sources)"
    r0, r1 = int(rows[0]), int(rows[-1]) + 1
    box = (x + c0, y + r0, x + c1, y + r1)
    sub = region[r0:r1, c0:c1]
    hb, wb = max(1, sub.shape[0] // block), max(1, sub.shape[1] // block)
    blocks = sub[:hb * block, :wb * block].reshape(hb, block, wb, block).transpose(0, 2, 1, 3).reshape(hb, wb, -1)
    textured = float((blocks.std(axis=2) > 6).mean()) if blocks.size else 0.0
    conf = min(1.0, 0.55 + 0.45 * textured)
    note = "content rectangle in the preview band" if textured > 0.05 else "preview container found but its content is flat (black / no video)"
    return box, conf, note


def find_bars(frame: Image.Image, row_band: Box, min_len: int = 40, max_thick: int = 8, meter_min_len: int = 10) -> list[tuple[Box, str, float]]:
    """Horizontal slider tracks / level meters inside a row band: thin bright or coloured horizontal runs.
    Returns (box, kind, confidence) with kind in {"meter", "track"}."""
    x, y, x2, y2 = row_band
    arr = _np(frame)[y:y2, x:x2]
    if arr.size == 0:
        return []
    lum = arr.mean(axis=2)
    bg = float(np.median(lum))
    # slider tracks are a slightly lighter grey than the bar; icons and knobs are near-white and thick, so they are excluded
    track_mask = (lum > bg + 12) & (lum < bg + 110)
    green = saturated_mask(arr, "green")
    out: list[tuple[Box, str, float]] = []
    used = np.zeros_like(track_mask)
    for mask, kind, need in ((green, "meter", meter_min_len), (track_mask & ~green, "track", min_len)):
        for yy in range(mask.shape[0]):
            row = mask[yy] & ~used[yy]
            if row.sum() < need:
                continue
            xs = np.nonzero(row)[0]
            # split into runs
            start = xs[0]; prev = xs[0]
            for xx in list(xs[1:]) + [None]:
                if xx is None or xx - prev > 3:
                    length = prev - start + 1
                    if length >= need:
                        # thickness: count consecutive rows below with the same run (a bar is thin; icons are not)
                        t = 1
                        while yy + t < mask.shape[0] and t <= max_thick * 3 and mask[yy + t, start:prev + 1].mean() > 0.6:
                            t += 1
                        used[yy:yy + t, start:prev + 1] = True
                        if t <= max_thick:
                            out.append(((x + int(start), y + yy, x + int(prev) + 1, y + yy + t), kind, 0.6 + 0.4 * min(1.0, length / 120)))
                    if xx is not None:
                        start = xx
                prev = xx if xx is not None else prev
    # merge overlapping boxes of the same kind
    merged: list[tuple[Box, str, float]] = []
    for box, kind, conf in sorted(out, key=lambda t: (t[0][1], t[0][0])):
        for i, (mb, mk, mc) in enumerate(merged):
            if mk == kind and abs(mb[1] - box[1]) <= max_thick and not (box[0] > mb[2] + 6 or box[2] < mb[0] - 6):
                merged[i] = ((min(mb[0], box[0]), min(mb[1], box[1]), max(mb[2], box[2]), max(mb[3], box[3])), kind, max(mc, conf))
                break
        else:
            merged.append((box, kind, conf))
    return merged


def find_filled_disc(frame: Image.Image, band: Box, size: tuple[int, int] = (14, 46), tol: int = 10) -> tuple[Optional[Box], float]:
    """A filled, roughly circular blob (the profile avatar) inside ``band``: pixels that differ from the bar colour
    form compact components; a disc has aspect ~1 and fills ~pi/4 of its box, unlike stroke icons (bell, gear).
    Prefers the right-most disc. Returns (box, confidence)."""
    x, y, x2, y2 = band
    arr = np.asarray(frame.convert("L").crop((x, y, x2, y2)), dtype=np.int16)
    if arr.size == 0:
        return None, 0.0
    bg = int(np.bincount(arr.ravel(), minlength=256).argmax())
    mask = (np.abs(arr - bg) > tol).astype(np.uint8)
    comps: list[tuple[int, int, int, int, int]] = []
    try:
        import cv2
        n, _lab, stats, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            cx, cy, cw, ch, area = (int(v) for v in stats[i])
            comps.append((cx, cy, cw, ch, area))
    except Exception:
        return None, 0.0
    best, best_conf = None, 0.0
    for cx, cy, cw, ch, area in comps:
        if not (size[0] <= cw <= size[1] and size[0] <= ch <= size[1]):
            continue
        aspect = cw / ch
        fill = area / float(cw * ch)
        if 0.75 <= aspect <= 1.33 and 0.6 <= fill <= 0.92:
            conf = 0.8 - abs(fill - 0.785) - abs(aspect - 1.0) * 0.3
            if best is None or cx > best[0]:                   # right-most disc on the title row
                best, best_conf = (cx, cy, cw, ch), max(0.5, conf)
    if best is None:
        return None, 0.0
    cx, cy, cw, ch = best
    return (x + cx, y + cy, x + cx + cw, y + cy + ch), round(min(1.0, best_conf), 2)


def find_avatar_circle(frame: Image.Image, band: Box, radius: tuple[int, int] = (8, 22)) -> tuple[Optional[Box], float]:
    """A small circular control (profile avatar) inside ``band``. Uses cv2 Hough circles when available, else a
    contrast-blob fallback. Returns (box, confidence)."""
    x, y, x2, y2 = band
    crop = frame.convert("L").crop((x, y, x2, y2))
    try:
        import cv2
        g = np.asarray(crop, dtype=np.uint8)
        g = cv2.GaussianBlur(g, (3, 3), 0)
        circles = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1.2, minDist=16, param1=90, param2=18, minRadius=radius[0], maxRadius=radius[1])
        if circles is not None and len(circles[0]):
            # prefer the right-most circle (profile control sits at the far right of the top bar)
            cx, cy, r = max(circles[0], key=lambda c: c[0])
            return (x + int(cx - r), y + int(cy - r), x + int(cx + r), y + int(cy + r)), 0.75
    except Exception:
        pass
    arr = np.asarray(crop, dtype=np.int16)
    bg = int(np.median(arr))
    mask = np.abs(arr - bg) > 40
    cols = np.nonzero(mask.mean(axis=0) > 0.3)[0]
    if len(cols) == 0:
        return None, 0.0
    # right-most compact run of columns
    runs = []
    start = cols[0]; prev = cols[0]
    for c in list(cols[1:]) + [None]:
        if c is None or c - prev > 2:
            runs.append((start, prev));
            if c is not None: start = c
        prev = c if c is not None else prev
    for s, e in reversed(runs):
        w = e - s + 1
        if radius[0] * 2 <= w <= radius[1] * 2 + 4:
            rows = np.nonzero(mask[:, s:e + 1].mean(axis=1) > 0.3)[0]
            if len(rows) and abs((rows[-1] - rows[0] + 1) - w) <= w * 0.4:
                return (x + int(s), y + int(rows[0]), x + int(e) + 1, y + int(rows[-1]) + 1), 0.55
    return None, 0.0


def grow_panel(frame: Image.Image, box: Box, tol: int = 10, max_grow: int = 700, fill: float = 0.85) -> Box:
    """Expand a text block to the uniform panel it sits on (a modal or banner background): the panel colour is the
    most common luminance inside the block; each side grows while the next pixel line is mostly that colour."""
    lum = np.asarray(frame.convert("L"), dtype=np.int16)
    h, w = lum.shape
    x, y, x2, y2 = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
    if x2 <= x or y2 <= y:
        return box
    inner = lum[y:y2, x:x2]
    panel = int(np.bincount(inner.ravel(), minlength=256).argmax())

    def ok(line: np.ndarray) -> bool:
        return line.size > 0 and float((np.abs(line - panel) <= tol).mean()) >= fill

    def grow(get_line, pos: int, step: int, limit: int, skip: int = 60) -> int:
        """Advance ``pos`` by ``step`` while lines match the panel; a short run of non-panel lines (buttons, icons,
        a second text column) is skipped when panel-coloured lines resume within ``skip`` pixels."""
        g = 0
        while 0 <= pos + step <= limit and g < max_grow:
            if ok(get_line(pos + step)):
                pos += step; g += 1
                continue
            jumped = False
            for k in range(2, skip + 1):
                q = pos + step * k
                if not (0 <= q <= limit):
                    break
                if ok(get_line(q)) and ok(get_line(q + step if 0 <= q + step <= limit else q)):
                    pos = q; g += k; jumped = True
                    break
            if not jumped:
                break
        return pos

    x = grow(lambda i: lum[y:y2, i], x, -1, w - 1) if x > 0 else x
    x2 = grow(lambda i: lum[y:y2, i - 1], x2, 1, w) if x2 < w else x2
    y = grow(lambda i: lum[i, x:x2], y, -1, h - 1) if y > 0 else y
    y2 = grow(lambda i: lum[i - 1, x:x2], y2, 1, h) if y2 < h else y2
    return x, y, x2, y2


def frame_signature(frame: Image.Image) -> np.ndarray:
    return np.asarray(frame.convert("L").resize((48, 27), Image.BILINEAR), dtype=np.float32) / 255.0
