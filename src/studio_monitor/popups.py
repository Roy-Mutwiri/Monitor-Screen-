"""Structured popup understanding for one frame.

A popup is a credible Studio UI dialog or banner: a spatially grouped text
block (heading / body / button row) sitting on a uniform panel in the centre
band of the window, or a banner row near the top of the preview. Text in the
chat panel, the title chip or the video itself never qualifies, even when it
contains words such as "LIVE" or "Go LIVE".

Each popup is classified into a typed category with its title, body, button
labels, bounding box, frame id, observation time, evidence and reason. The
classifier never strengthens wording: a "LIVE access suspended" notice is a
LIVE-access suspension, not an account suspension. Unfamiliar dialogs are
"unknown" and are reported for review, never guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image

from .broadcast import phrase_in
from .detection.rules import RuleSet, normalize_text
from .perception.anchors import grow_panel
from .perception.clusters import BUTTON_WORDS, TextBlock, attach_button_rows, group_blocks, is_button_box, merge_rows
from .perception.ocr_boxes import OcrBox

Box = tuple[int, int, int, int]

END_CONFIRMATION = "end_stream_confirmation"
LIVE_RESTRICTION = "live_restriction"
LIVE_ACCESS_SUSPENSION = "live_access_suspension"
ACCOUNT_SUSPENSION = "account_suspension"
VERIFICATION = "verification_challenge"
RECONNECTING = "reconnecting"
MISSING_SOURCE = "missing_source"
POST_LIVE_SUMMARY = "post_live_summary"
INFORMATIONAL = "informational"
SIGN_IN = "sign_in_screen"
STUDIO_SCREEN = "studio_screen"       # a full Studio page/sheet (LIVE settings, go-LIVE setup): main UI, never a popup
UNKNOWN = "unknown"

ALERTING_TYPES = (END_CONFIRMATION, LIVE_RESTRICTION, LIVE_ACCESS_SUSPENSION, ACCOUNT_SUSPENSION, VERIFICATION, RECONNECTING,
                  MISSING_SOURCE, UNKNOWN)
# popup type -> studio_rules.json category used by the restriction incident path (None = handled elsewhere)
RULE_CATEGORY = {LIVE_RESTRICTION: "restriction_notice", LIVE_ACCESS_SUSPENSION: "account_suspension", ACCOUNT_SUSPENSION: "account_suspension",
                 VERIFICATION: "verification_puzzle"}

POST_LIVE_PHRASES = ["that's a wrap", "thats a wrap", "how was your live experience", "total views", "joined your live via", "live has ended",
                     "your live ended", "live summary", "est this live"]
LIVE_ACCESS_PHRASES = ["live access", "access to live", "go live has been", "live feature", "live privileges", "cannot go live", "can no longer go live",
                      "not able to go live", "live is temporarily unavailable"]
INFO_BUTTONS = {"ok", "got it", "close", "check", "later", "learn more", "done", "dismiss", "continue"}
# Studio's pre-LIVE settings sheet (header 'LIVE settings', tabs LIVE info / Moderators, About me, Video settings,
# 'A camera source is required ...', 'run a network speed test ...'): a page the operator opens, not a popup
STUDIO_SCREEN_PHRASES = ["live settings", "live info moderators", "about me", "video settings", "add camera source",
                         "camera source is required", "run a network speed test", "lets go live", "live match", "studio view"]
# Studio's sign-in screen is a full page of cards (QR code, Google, email/password, confirm on mobile): one screen,
# never a set of dialogs to review one by one
SIGN_IN_PHRASES = ["scan to log in", "log in with", "continue with google", "continue with facebook", "continue with apple",
                   "email or username", "confirm on mobile app", "scan the qr code", "my qr code", "use phone / email", "forgot password",
                   "log in to tiktok", "sign up", "create account"]


@dataclass
class PopupObservation:
    popup_type: str
    title: str
    body: str
    button_labels: list[str]
    bounding_box: Box
    observed_at: float                 # frame capture time (wall clock)
    frame_id: int
    confidence: float
    classification_reason: str
    evidence: list[str] = field(default_factory=list)
    rule_category: str = ""            # studio_rules category for restriction-type popups
    kind: str = "dialog"               # dialog | banner
    severity_hint: str = ""

    @property
    def text(self) -> str:
        return "\n".join(x for x in (self.title, self.body, " / ".join(self.button_labels)) if x)

    def to_dict(self) -> dict:
        return {"popup_type": self.popup_type, "title": self.title, "body": self.body, "button_labels": list(self.button_labels),
                "bounding_box": list(self.bounding_box), "observed_at": self.observed_at, "frame_id": self.frame_id,
                "confidence": self.confidence, "classification_reason": self.classification_reason, "evidence": list(self.evidence),
                "rule_category": self.rule_category, "kind": self.kind}

    def fingerprint(self) -> str:
        return f"{self.popup_type}|{normalize_text(self.title)[:60]}|{normalize_text(self.body)[:120]}"


def _is_button(b: OcrBox) -> bool:
    return is_button_box(b)


def split_block(blk: TextBlock) -> tuple[str, str, list[str]]:
    """(title, body, buttons): the first non-button line is the title, following non-button lines the body."""
    buttons = [b.text.strip() for b in blk.lines if _is_button(b)]
    rest = [b for b in blk.lines if not _is_button(b)]
    title = rest[0].text.strip() if rest else ""
    body = " ".join(b.text.strip() for b in rest[1:])
    return title, body, buttons


def credible_dialog_text(title: str, body: str, buttons: list[str]) -> bool:
    """A dialog worth reporting as unknown must read like a message with a button row: a title of at least two
    alphabetic words, at least one button, and a body of three words or a second button. Feature tiles ('LIVE Goal'),
    counters ('52 Total'), badges ('Reward unlocked') and empty-state panels ('No highlights at the moment',
    'Add a cast source to share your screen', 'No video signal') fail this and are never reported."""
    words = lambda s: [w for w in re.findall(r"[A-Za-z][A-Za-z'’]+", s or "")]
    if not buttons:
        return False                                   # a dialog has a button row; empty-state panels and captions do not
    if len(words(title)) < 2:
        return False
    return len(words(body)) >= 3 or len(buttons) >= 2


class PopupClassifier:
    """``reocr`` (optional): callable(PIL image) -> list[OcrBox] used to rescan a dialog panel at 2x when the
    full-frame pass dropped part of it (white-on-red buttons such as 'End now' are the usual casualty)."""
    MAX_REOCR_PER_FRAME = 3

    def __init__(self, rules: RuleSet, end_rules, connection_rules=None, reocr=None) -> None:
        self.rules = rules
        self.end_rules = end_rules
        self.conn = connection_rules
        self.reocr = reocr
        self.last_reocr_ms = 0.0

    # ------------------------------------------------------------------ candidates
    def candidate_blocks(self, boxes: Iterable[OcrBox], frame: Image.Image, layout=None) -> list[tuple[TextBlock, Box, str]]:
        """Credible UI dialog/banner blocks: (block, panel box, kind). Text inside the chat panel, the title row and the
        side panels is excluded; the block must sit on a uniform panel (grown from the text) or be a top banner row."""
        w, h = frame.size
        band = (int(w * 0.12), int(h * 0.05), int(w * 0.88), int(h * 0.86))
        excluded: list[Box] = []
        if layout is not None:
            for key in ("chat_panel", "left_panel", "right_panel", "top_bar", "status_bar", "control_bar", "mixer"):
                el = layout.get(key)
                if el is not None:
                    excluded.append(el.box)
            pb = layout.get("preview_band")
            if pb is not None:
                band = (pb.box[0], pb.box[1], pb.box[2], max(pb.box[3], int(h * 0.9)))

        def inside(b: OcrBox, box: Box) -> bool:
            return box[0] <= b.cx <= box[2] and box[1] <= b.cy <= box[3]

        pool = [b for b in boxes if inside(b, band) and not any(inside(b, ex) for ex in excluded)]
        out: list[tuple[TextBlock, Box, str]] = []
        for blk in attach_button_rows(merge_rows(group_blocks(pool))):
            if len(blk.lines) < 2 and not blk.buttons:
                continue
            labels = {normalize_text(b.text) for b in blk.lines}
            if labels & {"go live", "end live", "start live"} and len(blk.lines) < 3:
                continue                                                  # the live control row, not a dialog
            panel = grow_panel(frame, (blk.x - 4, blk.y - 4, blk.x2 + 4, blk.y2 + 4))
            pw, ph = panel[2] - panel[0], panel[3] - panel[1]
            grew = pw > (blk.x2 - blk.x) + 16 and ph > (blk.y2 - blk.y) + 12
            if pw > w * 0.95 or ph > h * 0.95:
                continue                                                  # grew into the whole canvas: not a panel
            buttons = [b for b in blk.lines if _is_button(b)]
            heading = any(b.text.strip().endswith("?") for b in blk.lines)
            if not grew and not (buttons and len(blk.lines) >= 2):
                continue
            kind = "banner" if ph < h * 0.14 and blk.y < band[1] + h * 0.25 and len(blk.lines) <= 3 else "dialog"
            if kind == "dialog" and not (buttons or heading or len(blk.lines) >= 3):
                continue
            out.append((blk, panel, kind))
        return out

    # ------------------------------------------------------------------ classification
    def classify_text(self, title: str, body: str, buttons: list[str], kind: str) -> tuple[str, float, str, str]:
        """(popup_type, confidence, reason, rule_category) from the popup's own text only."""
        full = normalize_text(" ".join([title, body, " ".join(buttons)]))
        btns = [normalize_text(b) for b in buttons]
        if self.end_rules is not None:
            m = self.end_rules.match("\n".join([title, body] + buttons), [title, body] + buttons)
            if m:
                return END_CONFIRMATION, 0.95, "heading 'End streaming?' with End now/Cancel buttons", ""
        if any(phrase_in(p, full) for p in POST_LIVE_PHRASES):
            return POST_LIVE_SUMMARY, 0.85, "post-LIVE summary wording (wrap-up / total views / experience rating)", ""
        match = self.rules.match(full)
        if match is not None:
            cat = match.category.key
            if cat == "account_suspension":
                if any(phrase_in(p, full) for p in LIVE_ACCESS_PHRASES):
                    return LIVE_ACCESS_SUSPENSION, 0.85, f"notice limits LIVE access ('{(match.phrases[0] if match.phrases else '')}'), not the whole account", "account_suspension"
                if "account" in full:
                    return ACCOUNT_SUSPENSION, 0.85, f"account-level wording ('{(match.phrases[0] if match.phrases else '')}')", "account_suspension"
                return LIVE_ACCESS_SUSPENSION, 0.7, f"suspension wording without 'account' ('{(match.phrases[0] if match.phrases else '')}'); kept as LIVE-access", "account_suspension"
            if cat == "verification_puzzle":
                return VERIFICATION, 0.85, f"verification wording ('{(match.phrases[0] if match.phrases else '')}')", cat
            return LIVE_RESTRICTION, 0.8, f"{match.category.label.lower()} wording ('{(match.phrases[0] if match.phrases else '')}')", cat
        if self.conn is not None:
            c = self.conn.classify(full)
            if c.get("reconnecting"):
                return RECONNECTING, 0.8, f"connection wording ('{c['reconnecting']}')", ""
            if c.get("source_missing") or "audio settings" in full or "not available" in full and ("audio" in full or "camera" in full or "device" in full):
                return MISSING_SOURCE, 0.75, "missing source / audio-device wording", ""
        if any(phrase_in(p, full) for p in SIGN_IN_PHRASES):
            return SIGN_IN, 0.8, "sign-in screen wording (QR / Google / email-password / confirm on mobile)", ""
        if any(phrase_in(p, full) for p in STUDIO_SCREEN_PHRASES):
            return STUDIO_SCREEN, 0.8, "Studio page/sheet wording (LIVE settings / go-LIVE setup)", ""
        if btns and all(b in INFO_BUTTONS for b in btns) and len(full) < 400:
            return INFORMATIONAL, 0.6, "generic dialog with only acknowledgement buttons", ""
        return UNKNOWN, 0.5, "dialog wording matches no known category", ""

    @staticmethod
    def _floats_centred(panel: Box, size: tuple[int, int]) -> bool:
        """Studio modals float centred over the window; anything touching the window edge or sitting well off-centre
        is a docked panel (source list, tools, engage) and is never reported as an unknown dialog."""
        w, h = size
        x, y, x2, y2 = panel
        if x <= 2 or x2 >= w - 2 or y2 >= h - 2 or y <= 2:
            return False
        if (x2 - x) > 0.6 * w or (y2 - y) > 0.6 * h:
            return False                                        # a modal is compact; this is the canvas or a page
        return abs((x + x2) / 2 - w / 2) <= 0.2 * w

    @staticmethod
    def _dialog_shaped(panel: Box, size: tuple[int, int], blk: TextBlock) -> bool:
        """Studio modals are wider than tall and at least ~15% of the window wide, and their lines differ. A narrow
        column of repeated rows ('BTCUSD, buy 2.50 ...' from a trading terminal shown in the preview, 2026-10-09) is a
        table inside the video, never a dialog."""
        w, h = size
        x, y, x2, y2 = panel
        pw, ph = x2 - x, y2 - y
        if pw < 0.15 * w or pw < 0.8 * ph:
            return False
        norms = [normalize_text(b.text) for b in blk.lines]
        if len(norms) - len(set(norms)) >= 1:
            return False
        return True

    @staticmethod
    def _on_ui_surface(frame: Image.Image, panel: Box, layout=None) -> bool:
        """Studio draws its dialogs in the app's own surface colour (dark theme ~rgb 20-50, light theme near white).
        A panel whose colour is far from the window chrome (title bar) or is saturated is video / ad content inside
        the preview (e.g. 'DENTIST RECOMMENDED ... LEARN MORE' seen on 2026-10-09), never a dialog to review."""
        import numpy as np
        w, h = frame.size
        tb = layout.get("top_bar") if layout is not None else None
        cx0, cy0, cx1, cy1 = (tb.box if tb is not None else (0, 0, w, max(8, int(h * 0.05))))
        chrome = np.asarray(frame.convert("L").crop((cx0, cy0, cx1, cy1)), dtype=np.int16)
        x, y, x2, y2 = panel
        rgb = np.asarray(frame.crop((max(0, x), max(0, y), min(w, x2), min(h, y2))), dtype=np.int16).reshape(-1, 3)
        if rgb.size == 0 or chrome.size == 0:
            return True
        lum = np.median(rgb.mean(axis=1))
        sat = (rgb.max(axis=1) - rgb.min(axis=1)).mean()
        return bool(abs(float(lum) - float(np.median(chrome))) <= 60 and sat < 25)

    def _rescan_panel(self, frame: Image.Image, panel: Box, blk: TextBlock) -> Optional[TextBlock]:
        """Re-OCR the dialog panel at 2x; returns a replacement block when the rescan reads at least as many lines."""
        if self.reocr is None:
            return None
        x, y, x2, y2 = panel
        w, h = frame.size
        if (x2 - x) * (y2 - y) > 0.4 * w * h or x2 - x < 40 or y2 - y < 24:
            return None
        import time as _t
        t0 = _t.perf_counter()
        try:
            crop = frame.crop((x, y, x2, y2))
            crop = crop.resize((crop.width * 2, crop.height * 2), Image.BICUBIC)
            found = list(self.reocr(crop) or [])
        except Exception:
            return None
        finally:
            self.last_reocr_ms += (_t.perf_counter() - t0) * 1000
        mapped = [OcrBox(b.text, x + b.x // 2, y + b.y // 2, max(1, b.w // 2), max(1, b.h // 2)) for b in found if b.text.strip()]
        blocks = attach_button_rows(merge_rows(group_blocks(mapped)))
        if not blocks:
            return None
        best = max(blocks, key=lambda b: len(b.lines))
        return best if len(best.lines) >= len(blk.lines) else None

    def classify_frame(self, frame: Image.Image, boxes: list[OcrBox], frame_id: int, observed_at: float, layout=None) -> list[PopupObservation]:
        out: list[PopupObservation] = []
        self.last_reocr_ms = 0.0
        rescans = 0
        for blk, panel, kind in self.candidate_blocks(boxes, frame, layout):
            title, body, buttons = split_block(blk)
            ptype, conf, reason, rule_cat = self.classify_text(title, body, buttons, kind)
            if kind == "dialog" and ptype in (UNKNOWN, INFORMATIONAL) and rescans < self.MAX_REOCR_PER_FRAME:
                rescans += 1
                better = self._rescan_panel(frame, panel, blk)
                if better is not None:
                    t2, b2, btn2 = split_block(better)
                    p2, c2, r2, rc2 = self.classify_text(t2, b2, btn2, kind)
                    if p2 not in (UNKNOWN, INFORMATIONAL) or len(better.lines) > len(blk.lines):
                        blk, title, body, buttons = better, t2, b2, btn2
                        ptype, conf, reason, rule_cat = p2, c2, r2 + " (panel rescan)", rc2
            if ptype in (UNKNOWN, INFORMATIONAL) and not credible_dialog_text(title, body, buttons):
                continue                                                  # tile / counter / badge, not a message
            if ptype in (UNKNOWN, INFORMATIONAL) and not self._floats_centred(panel, frame.size):
                continue                                                  # docked side/bottom panel (sources, tools), not a modal
            if ptype in (UNKNOWN, INFORMATIONAL) and not self._on_ui_surface(frame, panel, layout):
                continue                                                  # text on video/ad content, not a Studio surface
            if ptype in (UNKNOWN, INFORMATIONAL) and not self._dialog_shaped(panel, frame.size, blk):
                continue                                                  # a narrow column / repeated rows: a list in the video, not a dialog
            out.append(PopupObservation(ptype, title, body, buttons, panel, observed_at, frame_id, conf, reason,
                                        evidence=blk.line_texts[:8], rule_category=rule_cat, kind=kind))
        if any(p.popup_type == POST_LIVE_SUMMARY for p in out):
            # the post-LIVE summary is a full-screen overlay made of several cards (rewards, top gifters, rating):
            # its other cards are parts of the summary, not new dialogs to review
            out = [p for p in out if p.popup_type not in (UNKNOWN, INFORMATIONAL)]
        w, h = frame.size
        loose = [p for p in out if p.popup_type in (UNKNOWN, INFORMATIONAL)]
        screen_type = next((t for t in (SIGN_IN, STUDIO_SCREEN) if any(p.popup_type == t for p in out)), None)
        if screen_type is None and len(loose) >= 3:
            ys = [p.bounding_box[1] for p in loose] + [p.bounding_box[3] for p in loose]
            if max(ys) - min(ys) > 0.5 * h:
                screen_type = STUDIO_SCREEN                      # three+ unrelated blocks spread over the window: a page, not dialogs
        if screen_type is not None:
            # a page (sign-in, LIVE settings sheet, go-LIVE setup): every card on it belongs to one screen
            cards = [p for p in out if p.popup_type in (screen_type, UNKNOWN, INFORMATIONAL)]
            keep = [p for p in out if p not in cards]
            x, y = min(c.bounding_box[0] for c in cards), min(c.bounding_box[1] for c in cards)
            x2, y2 = max(c.bounding_box[2] for c in cards), max(c.bounding_box[3] for c in cards)
            name = "Sign-in screen" if screen_type == SIGN_IN else "Studio page / sheet"
            one = PopupObservation(screen_type, name, " / ".join(c.title for c in cards if c.title)[:300],
                                   sorted({b for c in cards for b in c.button_labels})[:8], (x, y, x2, y2), observed_at, frame_id, 0.8,
                                   f"{name.lower()} wording across its cards", evidence=[c.title for c in cards][:8], kind="screen")
            out = keep + [one]
        return out

    def classify_lines(self, lines: list[str], frame_id: int, observed_at: float, frame_size: tuple[int, int]) -> list[PopupObservation]:
        """Geometry-free fallback (OCR backends without boxes): only the end-stream dialog is recognised from line
        adjacency; other categories keep the whole-frame restriction rules path."""
        if self.end_rules is None:
            return []
        m = self.end_rules.match("\n".join(lines), lines)
        if not m:
            return []
        # evidence = the dialog's own lines: from the heading line, within the rule's line span, only phrase matches
        phrases = list(self.end_rules.heading) + list(self.end_rules.body) + list(self.end_rules.confirm) + list(self.end_rules.cancel)
        norms = [normalize_text(l) for l in lines]
        start = next((i for i, n in enumerate(norms) if any(phrase_in(p, n) for p in self.end_rules.heading)), 0)
        span = getattr(self.end_rules, "max_line_span", 6)
        window = [(l, n) for l, n in zip(lines[start:start + span + 1], norms[start:start + span + 1])]
        evidence = [l for l, n in window if any(phrase_in(p, n) for p in phrases)]
        buttons = [l for l in evidence if _is_button(OcrBox(l, 0, 0, 1, 1))]
        return [PopupObservation(END_CONFIRMATION, "End streaming?", "End LIVE? Share your LIVE for more viewers.", buttons or ["End now", "Cancel"],
                                 (0, 0, frame_size[0], frame_size[1]), observed_at, frame_id, 0.8, "line-adjacency match (no OCR geometry)",
                                 evidence=evidence[:6], kind="dialog")]
