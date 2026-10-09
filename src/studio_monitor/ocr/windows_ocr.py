"""Windows built-in OCR (Windows.Media.Ocr) via the ``winocr`` package."""
from __future__ import annotations

from PIL import Image

from .base import OcrError, OcrResult, prepare


class WindowsOcr:
    name = "windows"

    def __init__(self, language: str = "en", upscale: float = 1.0) -> None:
        import winocr  # noqa: F401  (raises ImportError if unavailable)
        self.language = language or "en"
        self.upscale = upscale

    @staticmethod
    def is_available() -> bool:
        try:
            import winocr  # noqa: F401
            return True
        except Exception:
            return False

    def recognize(self, image: Image.Image) -> OcrResult:
        import winocr
        img = prepare(image, self.upscale, min_width=600)
        try:
            data = winocr.recognize_pil_sync(img, self.language)
        except Exception as exc:  # pragma: no cover - depends on OS language packs
            raise OcrError(f"Windows OCR failed: {exc}") from exc
        lines = [ln.get("text", "") for ln in data.get("lines", [])]
        text = data.get("text") or "\n".join(lines)
        return OcrResult(text=text, lines=[ln for ln in lines if ln], backend=self.name)
