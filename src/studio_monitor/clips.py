"""Optional incident clips: a short ring buffer of already-redacted frames
(the same frames the frame cache sees) written as an animated GIF when an
incident is raised. Off by default; frames never leave the redaction path."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image


@dataclass
class ClipsConfig:
    enabled: bool = False
    seconds_before: float = 10.0
    fps: float = 1.0
    max_width: int = 640
    send: bool = True          # attach to the Telegram alert (sendAnimation) when available


class ClipBuffer:
    def __init__(self, cfg: ClipsConfig, mono: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.mono = mono
        self._frames: deque[tuple[float, Image.Image]] = deque()
        self._last_add = -1e9

    def add(self, image: Image.Image) -> None:
        if not self.cfg.enabled:
            return
        now = self.mono()
        if now - self._last_add < 1.0 / max(0.1, self.cfg.fps):
            return
        w = min(self.cfg.max_width, image.width)
        small = image.convert("RGB").resize((w, max(1, int(image.height * w / max(1, image.width)))), Image.BILINEAR)
        self._frames.append((now, small))
        self._last_add = now
        cutoff = now - self.cfg.seconds_before
        while self._frames and self._frames[0][0] < cutoff:
            self._frames.popleft()

    def __len__(self) -> int:
        return len(self._frames)

    def write(self, dest: Path) -> Optional[str]:
        """GIF of the buffered frames; None when disabled or empty."""
        if not self.cfg.enabled or len(self._frames) < 2:
            return None
        frames = [f for _t, f in self._frames]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.gif")
        frames[0].save(tmp, format="GIF", save_all=True, append_images=frames[1:], duration=int(1000 / max(0.1, self.cfg.fps)), loop=0)
        tmp.replace(dest)
        return str(dest)
