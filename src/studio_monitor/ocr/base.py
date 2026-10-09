from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image


class OcrError(RuntimeError):
    pass


@dataclass
class OcrResult:
    text: str
    lines: list[str] = field(default_factory=list)
    backend: str = ""


class OcrBackend(Protocol):
    name: str

    def recognize(self, image: Image.Image) -> OcrResult: ...


def prepare(image: Image.Image, upscale: float = 1.0, min_width: int = 0) -> Image.Image:
    """Common preprocessing: RGB, optional upscale (small dialogs OCR better bigger)."""
    img = image.convert("RGB")
    scale = max(1.0, float(upscale or 1.0))
    if min_width and img.width < min_width:
        scale = max(scale, min_width / img.width)
    if scale > 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img
