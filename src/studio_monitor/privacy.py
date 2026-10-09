"""Privacy controls: redaction of user-defined regions, bounded text, local
screenshot retention."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .config import PrivacyConfig
from .regions import Region

log = logging.getLogger(__name__)


def redact(image: Image.Image, regions: list[Region]) -> Image.Image:
    """Black out every ``redact`` region. Applied before OCR and before any
    screenshot leaves the machine, so redacted content is never read or sent."""
    boxes = [r.to_box(*image.size) for r in regions if r.kind == "redact"]
    if not boxes:
        return image
    out = image.copy()
    draw = ImageDraw.Draw(out)
    for box in boxes:
        draw.rectangle(box, fill=(0, 0, 0))
    return out


def bounded_text(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def purge_old_screenshots(directory: Path, cfg: PrivacyConfig, now: float | None = None) -> int:
    """Delete screenshots older than the retention window. Returns count removed."""
    if cfg.screenshot_retention_days <= 0 or not directory.exists():
        return 0
    now = time.time() if now is None else now
    cutoff = now - cfg.screenshot_retention_days * 86400
    removed = 0
    for path in directory.glob("*.png"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover
            log.warning("could not remove %s: %s", path, exc)
    return removed
