"""RapidOCR backend (optional; pure pip, bundles ONNX models)."""
from __future__ import annotations

from PIL import Image

from .base import OcrError, OcrResult, prepare


class RapidOcr:
    name = "rapidocr"

    def __init__(self, upscale: float = 1.0) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()
        self.upscale = upscale

    @staticmethod
    def is_available() -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return True
        except Exception:
            return False

    def recognize(self, image: Image.Image) -> OcrResult:
        import numpy as np
        img = prepare(image, self.upscale, min_width=600)
        try:
            result, _ = self._engine(np.asarray(img))
        except Exception as exc:  # pragma: no cover
            raise OcrError(f"RapidOCR failed: {exc}") from exc
        lines = [item[1] for item in (result or []) if len(item) > 1]
        return OcrResult(text="\n".join(lines), lines=lines, backend=self.name)
