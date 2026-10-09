"""Tesseract backend (optional; requires the tesseract binary and pytesseract)."""
from __future__ import annotations

import shutil

from PIL import Image

from .base import OcrError, OcrResult, prepare

_LANG_MAP = {"en": "eng"}


class TesseractOcr:
    name = "tesseract"

    def __init__(self, language: str = "en", upscale: float = 1.0) -> None:
        import pytesseract  # noqa: F401
        self.language = _LANG_MAP.get(language, language)
        self.upscale = upscale

    @staticmethod
    def is_available() -> bool:
        try:
            import pytesseract  # noqa: F401
        except Exception:
            return False
        return shutil.which("tesseract") is not None

    def recognize(self, image: Image.Image) -> OcrResult:
        import pytesseract
        img = prepare(image, self.upscale, min_width=800)
        try:
            text = pytesseract.image_to_string(img, lang=self.language)
        except Exception as exc:  # pragma: no cover
            raise OcrError(f"Tesseract failed: {exc}") from exc
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return OcrResult(text="\n".join(lines), lines=lines, backend=self.name)
