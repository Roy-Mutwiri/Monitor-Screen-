"""Synthetic Studio frame renderer + an OCR double that returns the boxes of the text it drew.
The layout mimics the real Studio frame observed on 2026-10-09 (left sources/tools panel, centre program preview
with a control bar and a red Go LIVE/End LIVE button, right LIVE performance / LIVE chat panels, top bar with the
profile avatar circle, bottom status row) but every coordinate is a parameter so tests can resize, rearrange,
drop panels, add thumbnails/avatars or overlay dialogs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from studio_monitor.ocr.base import OcrResult
from studio_monitor.perception.ocr_boxes import OcrBox


def _font(size: int):
    try:
        return ImageFont.truetype("segoeui.ttf", size)
    except OSError:
        return ImageFont.load_default()


@dataclass
class StudioScene:
    width: int = 1512
    height: int = 726
    scale: float = 1.0                 # DPI-like scale for text/control sizes
    live: bool = False
    timer: str = "00:12:34"
    left_panel: bool = True
    right_panel: bool = True
    chat: bool = True
    preview_content: str = "video"     # video | black | portrait
    face_boxes: list = field(default_factory=list)      # (x, y, w, h) in *preview* pixels; drawn as magenta markers
    thumbnails: int = 0                # source thumbnails with face markers in the left panel
    chat_avatars: int = 2
    dialog: Optional[list] = None      # lines of a centred dialog
    banner: Optional[str] = None
    meter_lit: float = 0.5             # fraction of the level meter lit (0 = dark)
    lang: str = "en"
    layout_swap: bool = False          # swap left/right panels
    background: tuple = (16, 16, 18)
    noise: int = 3


class RenderedFrame:
    def __init__(self, image: Image.Image, boxes: list[OcrBox], elements: dict):
        self.image, self.boxes, self.elements = image, boxes, elements


def render(sc: StudioScene) -> RenderedFrame:
    import numpy as np
    W, H, s = sc.width, sc.height, sc.scale
    img = Image.new("RGB", (W, H), sc.background)
    d = ImageDraw.Draw(img)
    boxes: list[OcrBox] = []
    elements: dict = {}
    f = _font(int(13 * s)); fb = _font(int(15 * s))

    def text(t: str, x: int, y: int, font=f, fill=(230, 230, 230)):
        d.text((x, y), t, font=font, fill=fill)
        bb = d.textbbox((x, y), t, font=font)
        boxes.append(OcrBox(t, bb[0], bb[1], max(1, bb[2] - bb[0]), max(1, bb[3] - bb[1])))
        return bb

    top_h = int(46 * s)
    d.rectangle((0, 0, W, top_h), fill=(22, 22, 24))
    text("TikTok LIVE Studio", int(44 * s), int(14 * s), fb)
    text(("Lets Go LIVE! · Day Trader" if not sc.live else "Streaming · Day Trader"), W // 2 - int(90 * s), int(14 * s))
    lc_bb = text("LIVE Center", W - int(420 * s), int(14 * s))
    # profile avatar circle (dark disc with a light ring so it is visible), left of the window buttons
    ax, ay, ar = W - int(190 * s), top_h // 2, int(11 * s)
    d.ellipse((ax - ar, ay - ar, ax + ar, ay + ar), fill=(70, 70, 90), outline=(190, 190, 200), width=2)
    elements["profile_control"] = (ax - ar, ay - ar, ax + ar, ay + ar)
    for i, cx in enumerate((W - int(120 * s), W - int(80 * s), W - int(40 * s))):
        d.rectangle((cx - 6, ay - 6, cx + 6, ay + 6), outline=(200, 200, 200))

    left_w = int(320 * s) if sc.left_panel else 0
    right_w = int(320 * s) if sc.right_panel else 0
    lx0, lx1 = (W - left_w, W) if sc.layout_swap else (0, left_w)
    rx0, rx1 = (0, right_w) if sc.layout_swap else (W - right_w, W)
    status_h = int(28 * s)
    status_y = H - status_h
    if sc.left_panel:
        d.rectangle((lx0 + 8, top_h + 8, lx1 - 8, status_y - 8), fill=(28, 28, 31))
        text("Studio view", lx0 + int(24 * s), top_h + int(18 * s), fb)
        text("General", lx0 + int(24 * s), top_h + int(94 * s))
        y = top_h + int(126 * s)
        for name in ("msedge.exe", "TradingView.exe", "Camera"):
            text(name, lx0 + int(50 * s), y); y += int(34 * s)
        for i in range(sc.thumbnails):
            tx, ty = lx0 + int(24 * s), y + i * int(70 * s)
            d.rectangle((tx, ty, tx + int(96 * s), ty + int(54 * s)), fill=(60, 70, 80))
            d.rectangle((tx + 30, ty + 10, tx + 60, ty + 44), fill=(255, 0, 255))          # a face marker in a thumbnail
            elements.setdefault("thumbnails", []).append((tx, ty, tx + int(96 * s), ty + int(54 * s)))
        y += sc.thumbnails * int(70 * s)
        text("Add source", lx0 + int(130 * s), y + int(10 * s))
        text("Tools", lx0 + int(24 * s), y + int(60 * s), fb)
        text("Co-host", lx0 + int(40 * s), y + int(110 * s)); text("Multi-guest", lx0 + int(130 * s), y + int(110 * s))
        elements["left_panel"] = (lx0, top_h, lx1, status_y)
    if sc.right_panel:
        d.rectangle((rx0 + 8, top_h + 8, rx1 - 8, status_y - 8), fill=(28, 28, 31))
        text("LIVE performance", rx0 + int(16 * s), top_h + int(18 * s), fb)
        text("Creator Camp", rx0 + int(70 * s), top_h + int(120 * s))
        if sc.chat:
            cy = top_h + int(270 * s)
            text("LIVE chat", rx0 + int(16 * s), cy, fb)
            for i in range(sc.chat_avatars):
                ay2 = cy + int(60 * s) + i * int(40 * s)
                d.ellipse((rx0 + 16, ay2, rx0 + 16 + int(22 * s), ay2 + int(22 * s)), fill=(255, 0, 255))   # avatar face markers
                text(f"viewer{i} joined", rx0 + int(50 * s), ay2 + 4)
            text("Add comments during LIVE", rx0 + int(24 * s), status_y - int(40 * s))
            elements["chat_panel"] = (rx0, cy, rx1, status_y)
        elements["right_panel"] = (rx0, top_h, rx1, status_y)

    cx0, cx1 = (lx1 if not sc.layout_swap else rx1), (rx0 if not sc.layout_swap else lx0)
    ctrl_h = int(52 * s)
    ctrl_y = status_y - ctrl_h - int(10 * s)
    # program preview: a portrait or full video rectangle centred in the band
    band = (cx0 + 12, top_h + 8, cx1 - 12, ctrl_y - 8)
    if sc.preview_content == "black":
        pv = band
    elif sc.preview_content == "portrait":
        pw = int((band[3] - band[1]) * 9 / 16); mid = (band[0] + band[2]) // 2
        pv = (mid - pw // 2, band[1] + 10, mid + pw // 2, band[3] - 10)
    else:
        pv = (band[0] + 20, band[1] + 20, band[2] - 20, band[3] - 20)
    if sc.preview_content != "black":
        arr = np.random.RandomState(7).randint(40, 120, size=(pv[3] - pv[1], pv[2] - pv[0], 3)).astype("uint8")
        img.paste(Image.fromarray(arr), (pv[0], pv[1]))
        d = ImageDraw.Draw(img)
        for (fx, fy, fw, fh) in sc.face_boxes:
            d.rectangle((pv[0] + fx, pv[1] + fy, pv[0] + fx + fw, pv[1] + fy + fh), fill=(255, 0, 255))
    elements["program_preview"] = pv
    elements["preview_band"] = band
    if sc.live:
        text(f"LIVE {sc.timer}", pv[0] + 12, pv[1] + 10, fb, fill=(255, 255, 255))
    if sc.banner:
        bx0, by0 = cx0 + int(140 * s), top_h + int(30 * s)
        d.rectangle((bx0, by0, cx1 - int(140 * s), by0 + int(60 * s)), fill=(48, 48, 52))
        text(sc.banner, bx0 + 12, by0 + 12); text("Check", cx1 - int(230 * s), by0 + 14)
    if sc.dialog:
        dw, dh = int(360 * s), int(60 * s) + len(sc.dialog) * int(34 * s)
        dx0 = (cx0 + cx1) // 2 - dw // 2; dy0 = (band[1] + band[3]) // 2 - dh // 2
        d.rectangle((dx0, dy0, dx0 + dw, dy0 + dh), fill=(37, 37, 40))
        yy = dy0 + 18
        for i, line in enumerate(sc.dialog):
            if i == len(sc.dialog) - 1 and "|" in line:
                b1, b2 = line.split("|")
                d.rectangle((dx0 + 20, yy, dx0 + 160, yy + int(30 * s)), fill=(230, 38, 83)); text(b1.strip(), dx0 + 60, yy + 6)
                d.rectangle((dx0 + 180, yy, dx0 + 320, yy + int(30 * s)), fill=(58, 58, 62)); text(b2.strip(), dx0 + 225, yy + 6)
            else:
                text(line, dx0 + 20, yy, fb if i == 0 else f)
            yy += int(34 * s)
        elements["dialog"] = (dx0, dy0, dx0 + dw, dy0 + dh)
    # control bar with mixer and the live button
    d.rectangle((cx0 + 10, ctrl_y, cx1 - 10, ctrl_y + ctrl_h), fill=(26, 26, 29))
    mx = cx0 + int(450 * s)
    track_y = ctrl_y + ctrl_h // 2
    d.rectangle((mx, track_y - 2, mx + int(110 * s), track_y + 2), fill=(90, 90, 95))                     # slider track
    if sc.meter_lit > 0:
        d.rectangle((mx, track_y - 2, mx + int(110 * s * sc.meter_lit), track_y + 2), fill=(30, 200, 90))     # lit level
    elements["audio_meter"] = (mx, track_y - 4, mx + int(110 * s), track_y + 4)
    d.ellipse((mx + int(60 * s) - 6, track_y - 6, mx + int(60 * s) + 6, track_y + 6), fill=(240, 240, 240))
    mx2 = mx + int(150 * s)
    d.rectangle((mx2, track_y - 2, mx2 + int(110 * s), track_y + 2), fill=(90, 90, 95))
    bw, bh = int(96 * s), int(36 * s)
    bx = cx1 - 20 - bw; by = ctrl_y + (ctrl_h - bh) // 2
    d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=6, fill=(230, 38, 83))
    label = "End LIVE" if sc.live else "Go LIVE"
    tb = d.textbbox((0, 0), label, font=fb)
    text(label, bx + (bw - (tb[2] - tb[0])) // 2, by + (bh - (tb[3] - tb[1])) // 2 - 2, fb, fill=(255, 255, 255))
    elements["live_control"] = (bx, by, bx + bw, by + bh)
    elements["control_bar"] = (cx0, ctrl_y, cx1, ctrl_y + ctrl_h)
    # status row
    text("CPU: 1.1%", int(520 * s), status_y + 8); text("Memory: 0.1%", int(610 * s), status_y + 8)
    text("Upload: 0 kbps", int(710 * s), status_y + 8); text("FPS: 0/60", int(930 * s), status_y + 8)
    elements["status_bar"] = (0, status_y, W, H)
    if sc.noise:
        arr = np.asarray(img).astype("int16")
        arr += np.random.RandomState(1).randint(-sc.noise, sc.noise + 1, size=arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    return RenderedFrame(img, boxes, elements)


class BoxOcr:
    """OCR double: returns the boxes of drawn text that fall inside the requested image region.
    It is told the full-frame boxes and the crop offset via ``set_frame``; crops are matched by size+content."""
    name = "fake-boxes"

    def __init__(self, with_geometry: bool = True):
        self.frames: list[tuple[Image.Image, list[OcrBox]]] = []
        self.with_geometry = with_geometry
        self.calls = 0

    def set_frame(self, image: Image.Image, boxes: list[OcrBox]) -> None:
        self.frames = [(image, boxes)]

    def _lookup(self, image: Image.Image) -> list[OcrBox]:
        for full, boxes in self.frames:
            if image.size == full.size:
                return boxes
            # a crop: find its offset by matching the first row of pixels (cheap heuristic for tests)
            w, h = image.size
            for ox in range(0, full.width - w + 1, 8):
                for oy in range(0, full.height - h + 1, 8):
                    if full.getpixel((ox, oy)) == image.getpixel((0, 0)) and full.getpixel((ox + w - 1, oy + h - 1)) == image.getpixel((w - 1, h - 1)):
                        return [b.offset(-ox, -oy) for b in boxes if b.cx >= ox and b.cx <= ox + w and b.cy >= oy and b.cy <= oy + h]
        return []

    def recognize(self, image: Image.Image) -> OcrResult:
        self.calls += 1
        boxes = self._lookup(image)
        lines = [b.text for b in sorted(boxes, key=lambda b: (b.y, b.x))]
        return OcrResult("\n".join(lines), lines, self.name, boxes if self.with_geometry else [])

    def recognize_boxes(self, image: Image.Image) -> OcrResult:
        return self.recognize(image)
