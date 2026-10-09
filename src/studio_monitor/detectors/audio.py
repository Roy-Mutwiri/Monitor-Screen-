"""Audio activity from Studio's on-screen audio meter (calibrated region).

The signal measured is explicitly *Studio's meter as rendered in the
captured Studio window*, not system output. The level is the fraction of
the meter region whose pixels are "lit" relative to the region's own dark
baseline, so the result is UNKNOWN when the region has no readable contrast
(not calibrated, hidden, or covered). An optional local loopback backend is
not implemented in this version (documented); the meter reader is the only
signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

PROFILES = ("mixed", "mic-only", "music-only")


@dataclass
class AudioReading:
    valid: bool
    level: float = 0.0        # 0..1 lit fraction
    note: str = ""


def read_meter(frame: Image.Image, box: tuple[int, int, int, int], lit_delta: int = 60) -> AudioReading:
    """Lit fraction of a meter region. Pixels brighter than the darkest 10 %
    baseline + ``lit_delta`` count as lit (works for green/colour meters)."""
    region = frame.crop(box).convert("RGB")
    if region.width < 8 or region.height < 4:
        return AudioReading(False, note="audio meter region too small")
    arr = np.asarray(region, dtype=np.int16)
    luma = arr.max(axis=2)                     # brightest channel: coloured meter segments light up
    baseline = float(np.percentile(luma, 10))
    peak = float(np.percentile(luma, 99))
    if peak - baseline < lit_delta * 0.5:
        # no contrast at all: either silence on a readable meter or an unreadable region. Distinguish by
        # whether the region looks like a meter track (some structure) or flat noise/colour.
        std = float(luma.std())
        if std < 2.0:
            return AudioReading(False, level=0.0, note="meter region has no visible structure (not readable)")
        return AudioReading(True, level=0.0, note="meter dark")
    lit = float(np.mean(luma > baseline + lit_delta))
    return AudioReading(True, level=lit)
