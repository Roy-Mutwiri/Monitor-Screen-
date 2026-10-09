"""Spatial grouping of OCR boxes into text blocks (panels, banners, dialogs).

A block is a set of lines whose vertical gaps are small relative to their
height and whose horizontal extents overlap or sit close together. Dialogs
and banners are blocks that float over the centre band of the frame and
contain a short button-like line. This replaces "N consecutive OCR lines"
heuristics with geometry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .ocr_boxes import OcrBox

BUTTON_WORDS = {"ok", "cancel", "close", "end now", "check", "retry", "got it", "continue", "confirm", "dismiss", "later",
                "verify", "start", "reconnect", "yes", "no", "done", "learn more", "end live", "go live"}


@dataclass
class TextBlock:
    lines: list[OcrBox]
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0

    def __post_init__(self) -> None:
        self.x = min(b.x for b in self.lines); self.y = min(b.y for b in self.lines)
        self.x2 = max(b.x2 for b in self.lines); self.y2 = max(b.y2 for b in self.lines)

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x2, self.y2

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.lines)

    @property
    def line_texts(self) -> list[str]:
        return [b.text for b in self.lines]

    @property
    def buttons(self) -> list[OcrBox]:
        return [b for b in self.lines if b.text.strip().lower().rstrip("?.!") in BUTTON_WORDS or
                (len(b.text) <= 12 and b.text.strip().lower() in BUTTON_WORDS)]

    @property
    def line_height(self) -> float:
        return sum(b.h for b in self.lines) / len(self.lines)


def group_blocks(boxes: Iterable[OcrBox], v_gap: float = 1.6, h_slack: float = 1.5) -> list[TextBlock]:
    """Greedy top-down grouping: a line joins a block when its vertical distance to the block's last line is
    below ``v_gap`` line heights and the horizontal extents overlap (with ``h_slack`` line heights of slack)."""
    lines = sorted(boxes, key=lambda b: (b.y, b.x))
    blocks: list[list[OcrBox]] = []
    for b in lines:
        joined = False
        for blk in blocks:
            last = max(blk, key=lambda l: l.y2)
            lh = max(1.0, (last.h + b.h) / 2)
            vgap = b.y - last.y2
            if -lh * 0.5 <= vgap <= v_gap * lh:
                bx, bx2 = min(l.x for l in blk), max(l.x2 for l in blk)
                if b.x <= bx2 + h_slack * lh and b.x2 >= bx - h_slack * lh:
                    blk.append(b)
                    joined = True
                    break
        if not joined:
            blocks.append([b])
    return [TextBlock(blk) for blk in blocks]


def floating_blocks(blocks: Iterable[TextBlock], band: tuple[int, int, int, int], min_lines: int = 1) -> list[TextBlock]:
    """Blocks whose centre lies inside ``band`` (x, y, x2, y2) — the centre/preview area where modals and banners appear."""
    x, y, x2, y2 = band
    out = []
    for blk in blocks:
        cx, cy = (blk.x + blk.x2) / 2, (blk.y + blk.y2) / 2
        if x <= cx <= x2 and y <= cy <= y2 and len(blk.lines) >= min_lines:
            out.append(blk)
    return out


def merge_rows(blocks: list[TextBlock], max_gap_lines: float = 14.0) -> list[TextBlock]:
    """Join blocks that share a row (vertical overlap >= 50 %) and sit within ``max_gap_lines`` line heights of each
    other: a banner's text and its button are usually far apart horizontally but on one row."""
    blocks = sorted(blocks, key=lambda b: (b.y, b.x))
    merged: list[TextBlock] = []
    for blk in blocks:
        for i, m in enumerate(merged):
            overlap = min(blk.y2, m.y2) - max(blk.y, m.y)
            if overlap >= 0.5 * min(blk.y2 - blk.y, m.y2 - m.y) and (blk.x - m.x2) < max_gap_lines * m.line_height and blk.x >= m.x:
                merged[i] = TextBlock(m.lines + blk.lines)
                break
        else:
            merged.append(blk)
    return merged


def attach_button_rows(blocks: list[TextBlock], max_gap_lines: float = 4.0) -> list[TextBlock]:
    """A row made only of button words directly below a block (horizontally overlapping) belongs to that block."""
    blocks = sorted(blocks, key=lambda b: (b.y, b.x))
    out: list[TextBlock] = []
    for blk in blocks:
        if blk.lines and all(b in blk.buttons for b in blk.lines):
            for i, m in enumerate(out):
                gap = blk.y - m.y2
                if 0 <= gap <= max_gap_lines * m.line_height and blk.x <= m.x2 and blk.x2 >= m.x:
                    out[i] = TextBlock(m.lines + blk.lines)
                    break
            else:
                out.append(blk)
        else:
            out.append(blk)
    return out


def dialog_candidates(blocks: Iterable[TextBlock], band: Optional[tuple[int, int, int, int]] = None) -> list[TextBlock]:
    """Blocks that look like a dialog or banner: >= 2 text boxes and at least one button-like box (or a heading + body)."""
    pool = floating_blocks(blocks, band) if band else list(blocks)
    out = []
    for blk in attach_button_rows(merge_rows(pool)):
        if len(blk.lines) >= 2 and (blk.buttons or any(b.text.endswith("?") for b in blk.lines)):
            out.append(blk)
    return out
