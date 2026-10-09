"""Run OCR over captured windows/regions and apply the rules."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from ..ocr.base import OcrBackend, OcrError
from ..regions import FULL_WINDOW, Region
from ..win32.capture import Capture
from .rules import Match, RuleSet

log = logging.getLogger(__name__)


@dataclass
class Detection:
    match: Match
    capture: Capture          # the exact capture that triggered
    region: Region
    ocr_text: str

    @property
    def category(self) -> str:
        return self.match.key

    @property
    def is_dialog(self) -> bool:
        return self.capture.is_dialog


class Detector:
    def __init__(self, ocr: OcrBackend, rules: RuleSet, log_text: bool = False) -> None:
        self.ocr = ocr
        self.rules = rules
        self.log_text = log_text

    def scan_image(self, image: Image.Image, regions: list[Region]) -> list[tuple[Region, str, Optional[Match]]]:
        """OCR each region (or the whole image) and return (region, text, match)."""
        results = []
        for region in regions or [FULL_WINDOW]:
            if region.kind != "detect":
                continue
            crop = region.crop(image) if region is not FULL_WINDOW else image
            try:
                text = self.ocr.recognize(crop).text
            except OcrError as exc:
                log.warning("OCR failed for region %s: %s", region.name, exc)
                continue
            if self.log_text:
                log.debug("OCR[%s]: %r", region.name, text[:300])
            results.append((region, text, self.rules.match(text)))
        return results

    def detect(self, capture: Capture, regions: list[Region]) -> Optional[Detection]:
        """Return the highest-priority detection in this capture, if any."""
        best: Optional[Detection] = None
        for region, text, match in self.scan_image(capture.image, regions):
            if match is None:
                continue
            if best is None or match.category.priority > best.match.category.priority:
                best = Detection(match, capture, region, text)
        return best
