"""Latest-frame cache: the most recent *valid, redacted* Studio frame.

Used for the "Studio closed" notification (last frame before closure) and as
the "fresh screenshot" source for opened/reminder notices. It is kept apart
from restriction evidence (``screenshots/``) and persisted atomically so a
monitor crash never leaves a half-written image.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from PIL import Image


def utc_now_iso(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class CachedFrame:
    image: Image.Image
    captured_utc: str          # ISO 8601, UTC
    captured_at: float         # wall-clock epoch seconds (for age checks / display)
    captured_mono: float       # monotonic seconds (for freshness inside one process)
    path: str = ""


class FrameCache:
    PNG = "latest.png"
    META = "latest.json"

    def __init__(self, directory: Path, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic, persist_interval_seconds: float = 10.0) -> None:
        self.dir = Path(directory)
        self.clock = clock
        self.mono = mono
        self.persist_interval = persist_interval_seconds
        self._last_persist_mono = float("-inf")
        self._lock = threading.Lock()
        self._frame: Optional[CachedFrame] = None
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        """Pick up a frame persisted by a previous run (its timestamp is kept)."""
        png, meta = self.dir / self.PNG, self.dir / self.META
        if not (png.exists() and meta.exists()):
            return
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            img = Image.open(png)
            img.load()
            self._frame = CachedFrame(img, data["captured_utc"], float(data["captured_at"]),
                                      float("-inf"), str(png))
        except (OSError, ValueError, KeyError):
            self._frame = None

    def update(self, image: Image.Image, persist: Optional[bool] = None) -> CachedFrame:
        """Store an already-redacted frame in memory and, at most every
        ``persist_interval_seconds`` (or when ``persist`` is True), on disk.
        Disk writes go to temp files that are then renamed (atomic)."""
        now = self.clock()
        now_mono = self.mono()
        frame = CachedFrame(image.copy(), utc_now_iso(self.clock), now, now_mono, str(self.dir / self.PNG))
        if persist is None:
            persist = (now_mono - self._last_persist_mono) >= self.persist_interval
        with self._lock:
            self._frame = frame
            if persist:
                self._persist(frame)
                self._last_persist_mono = now_mono
        return frame

    def _persist(self, frame: CachedFrame) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        png, meta = self.dir / self.PNG, self.dir / self.META
        tmp_png, tmp_meta = self.dir / (self.PNG + ".tmp"), self.dir / (self.META + ".tmp")
        frame.image.save(tmp_png, format="PNG")
        tmp_meta.write_text(json.dumps({"captured_utc": frame.captured_utc, "captured_at": frame.captured_at}),
                            encoding="utf-8")
        os.replace(tmp_png, png)
        os.replace(tmp_meta, meta)

    def flush(self) -> None:
        """Persist the in-memory frame now (called when monitoring stops)."""
        with self._lock:
            if self._frame is not None:
                self._persist(self._frame)
                self._last_persist_mono = self.mono()

    def latest(self) -> Optional[CachedFrame]:
        with self._lock:
            return self._frame

    def fresh(self, max_age_seconds: float) -> Optional[CachedFrame]:
        """Frame captured within ``max_age_seconds`` by *this* process, else None."""
        with self._lock:
            f = self._frame
        if f is None or f.captured_mono == float("-inf"):
            return None
        return f if (self.mono() - f.captured_mono) <= max_age_seconds else None

    def export(self, dest: Path) -> Optional[str]:
        """Copy the latest frame to ``dest`` (so a later cache update cannot
        change the image attached to an alert). Returns the path or None."""
        f = self.latest()
        if f is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        f.image.save(tmp, format="PNG")
        os.replace(tmp, dest)
        return str(dest)

    def purge(self, retention_days: int, now: Optional[float] = None) -> bool:
        """Drop the cached frame when older than the retention window."""
        if retention_days <= 0:
            return False
        now = self.clock() if now is None else now
        with self._lock:
            f = self._frame
            if f is None or now - f.captured_at < retention_days * 86400:
                return False
            self._frame = None
        for name in (self.PNG, self.META):
            try:
                (self.dir / name).unlink()
            except OSError:
                pass
        return True

    def clear(self) -> None:
        with self._lock:
            self._frame = None
