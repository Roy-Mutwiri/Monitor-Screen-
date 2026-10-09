"""OCR backends.

``auto`` picks the first available of: Windows built-in OCR (no external
binary, ships with Windows 10/11), Tesseract (if installed), RapidOCR.
"""
from __future__ import annotations

from .base import OcrBackend, OcrResult, OcrError

__all__ = ["OcrBackend", "OcrResult", "OcrError", "create_backend", "available_backends"]


def available_backends() -> list[str]:
    names = []
    try:
        from .windows_ocr import WindowsOcr  # noqa: F401
        if WindowsOcr.is_available():
            names.append("windows")
    except Exception:
        pass
    try:
        from .tesseract_ocr import TesseractOcr  # noqa: F401
        if TesseractOcr.is_available():
            names.append("tesseract")
    except Exception:
        pass
    try:
        from .rapid_ocr import RapidOcr  # noqa: F401
        if RapidOcr.is_available():
            names.append("rapidocr")
    except Exception:
        pass
    return names


def create_backend(name: str = "auto", language: str = "en", upscale: float = 1.0) -> OcrBackend:
    if name == "auto":
        available = available_backends()
        if not available:
            raise OcrError(
                "No OCR backend available. Install the 'winocr' package (Windows OCR), "
                "Tesseract + pytesseract, or rapidocr-onnxruntime."
            )
        name = available[0]
    if name == "windows":
        from .windows_ocr import WindowsOcr
        return WindowsOcr(language, upscale)
    if name == "tesseract":
        from .tesseract_ocr import TesseractOcr
        return TesseractOcr(language, upscale)
    if name == "rapidocr":
        from .rapid_ocr import RapidOcr
        return RapidOcr(upscale)
    raise OcrError(f"Unknown OCR backend {name!r}")
