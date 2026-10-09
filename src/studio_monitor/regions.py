"""Detection and redaction regions, stored as fractions of the window size so
they keep pointing at the same part of Studio when it moves or resizes."""
from __future__ import annotations

from dataclasses import dataclass, asdict

from PIL import Image


@dataclass(frozen=True)
class Region:
    name: str
    x: float  # 0..1 fraction of width
    y: float
    w: float
    h: float
    kind: str = "detect"  # "detect" | "redact"

    def __post_init__(self) -> None:
        for field in ("x", "y", "w", "h"):
            v = getattr(self, field)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"Region {self.name}: {field}={v} must be within 0..1")
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Region {self.name}: width and height must be positive")
        if self.kind not in ("detect", "redact"):
            raise ValueError(f"Region {self.name}: unknown kind {self.kind!r}")

    def to_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Pixel box (left, top, right, bottom) for an image of the given size."""
        left = int(round(self.x * width))
        top = int(round(self.y * height))
        right = min(width, int(round((self.x + self.w) * width)))
        bottom = min(height, int(round((self.y + self.h) * height)))
        return left, top, max(left + 1, right), max(top + 1, bottom)

    def crop(self, image: Image.Image) -> Image.Image:
        return image.crop(self.to_box(*image.size))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Region":
        return cls(
            name=str(data.get("name", "region")),
            x=float(data["x"]), y=float(data["y"]),
            w=float(data["w"]), h=float(data["h"]),
            kind=str(data.get("kind", "detect")),
        )

    @classmethod
    def from_pixels(cls, name: str, box: tuple[int, int, int, int], size: tuple[int, int],
                    kind: str = "detect") -> "Region":
        left, top, right, bottom = box
        width, height = size
        left, right = sorted((max(0, left), min(width, right)))
        top, bottom = sorted((max(0, top), min(height, bottom)))
        return cls(name, left / width, top / height,
                   max(1, right - left) / width, max(1, bottom - top) / height, kind)


FULL_WINDOW = Region("full window", 0.0, 0.0, 1.0, 1.0)
