"""Continuous relocalization: a low-frequency discovery pass on a worker
thread (latest frame only, no backlog) plus cheap validation between passes.
Discovery is re-run when the window resizes, Studio restarts, the frame
changes wholesale (scene/panel change), anchors stop validating, the periodic
interval elapses, or the operator asks for it."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image

from .anchors import frame_signature
from .layout import Layout, LayoutStore, discover_layout, validate_layout
from .ocr_boxes import OcrBox, recognize_boxes

log = logging.getLogger(__name__)


@dataclass
class PerceptionStatus:
    state: str = "idle"              # idle | locating | detected | partly | failed
    layout_signature: str = ""
    last_discovery_at: float = 0.0
    last_reason: str = ""
    discoveries: int = 0
    last_ms: float = 0.0
    avg_ms: float = 0.0
    uia_note: str = ""
    ocr_geometry: bool = True
    notes: list[str] = field(default_factory=list)
    from_cache: bool = False
    elements: dict = field(default_factory=dict)      # type -> {box, confidence, source}
    transient: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"state": self.state, "signature": self.layout_signature, "last_discovery_at": self.last_discovery_at,
                "reason": self.last_reason, "discoveries": self.discoveries, "last_ms": round(self.last_ms, 1), "avg_ms": round(self.avg_ms, 1),
                "uia": self.uia_note, "ocr_geometry": self.ocr_geometry, "notes": self.notes[:6], "from_cache": self.from_cache,
                "elements": self.elements, "transient": self.transient}


class LayoutTracker:
    def __init__(self, ocr: Any, store: Optional[LayoutStore], *, uia_probe: Optional[Callable[[], tuple[list, str]]] = None,
                 discovery_interval: float = 60.0, validate_interval: float = 5.0, change_threshold: float = 0.25,
                 mono: Callable[[], float] = time.monotonic, clock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[str], None]] = None, studio_version: str = "", dpi_scale: float = 1.0,
                 extra_parser: Optional[Callable[[Image.Image], list]] = None, anchors: Optional[dict] = None) -> None:
        self.ocr = ocr
        self.store = store
        self.uia_probe = uia_probe or (lambda: ([], "no accessibility probe configured"))
        self.discovery_interval = discovery_interval
        self.validate_interval = validate_interval
        self.change_threshold = change_threshold
        self.mono, self.clock = mono, clock
        self.on_event = on_event or (lambda m: log.info(m))
        self.studio_version = studio_version
        self.dpi_scale = dpi_scale
        self.extra_parser = extra_parser
        self.anchors = anchors
        self.layout: Optional[Layout] = None
        self.status = PerceptionStatus()
        self.last_boxes: list[OcrBox] = []
        self.last_text: str = ""
        self._sig: Optional[np.ndarray] = None
        self._last_validate = 0.0
        self._pending: Optional[tuple[Image.Image, float, str]] = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = False
        self._pid = 0
        self.manual_override = False

    # ------------------------------------------------------------------ requests
    def request(self, frame: Image.Image, frame_ts: float, reason: str) -> None:
        """Queue the latest frame for a discovery pass (replaces any pending one: no backlog)."""
        with self._lock:
            self._pending = (frame.copy(), frame_ts, reason)
        if self._thread is not None:
            self._wake.set()

    def observe(self, frame: Optional[Image.Image], frame_ts: float, fresh: bool, pid: int = 0) -> None:
        """Called every poll with the latest frame. Decides cheaply whether a discovery pass is needed and runs the
        pass inline when no worker thread is running (tests / --once)."""
        now = self.mono()
        if frame is None or not fresh:
            return
        reason = ""
        if pid and self._pid and pid != self._pid:
            reason = "Studio restarted"
        self._pid = pid or self._pid
        if self.layout is None:
            reason = reason or "initial discovery"
        elif frame.size != self.layout.size:
            reason = reason or f"window resized to {frame.size[0]}x{frame.size[1]}"
        sig = frame_signature(frame)
        if self._sig is not None and self._sig.shape == sig.shape and float(np.mean(np.abs(sig - self._sig))) > self.change_threshold:
            reason = reason or "frame changed wholesale (scene/panel change)"
        self._sig = sig
        if not reason and self.layout is not None and now - self._last_validate >= self.validate_interval:
            self._last_validate = now
            ok, problems = validate_layout(self.layout, frame)
            if not ok:
                for el in self.layout.elements.values():
                    el.valid = False if any("live control" in p or "size" in p for p in problems) else el.valid
                reason = "anchors contradict the cached layout: " + "; ".join(problems)[:120]
            else:
                self.layout.validations += 1
        if not reason and self.layout is not None and now - self.status.last_discovery_at >= self.discovery_interval:
            reason = "periodic refresh"
        if reason:
            self.request(frame, frame_ts, reason)
        if self._thread is None:
            self.run_pending()

    # ------------------------------------------------------------------ discovery
    def run_pending(self) -> bool:
        with self._lock:
            job, self._pending = self._pending, None
        if job is None:
            return False
        frame, ts, reason = job
        self._busy = True
        self.status.state = "locating" if self.layout is None else self.status.state
        t0 = self.mono()
        try:
            self._discover(frame, ts, reason)
        except Exception as exc:  # never take the monitor down
            log.exception("layout discovery failed: %s", exc)
            self.status.state = "failed"
            self.status.notes = [f"discovery error: {exc}"[:160]]
        finally:
            self._busy = False
            ms = (self.mono() - t0) * 1000
            self.status.last_ms = ms
            n = self.status.discoveries
            self.status.avg_ms = (self.status.avg_ms * n + ms) / (n + 1)
            self.status.discoveries = n + 1
            self.status.last_discovery_at = self.mono()
            self.status.last_reason = reason
        return True

    def _discover(self, frame: Image.Image, ts: float, reason: str) -> None:
        text, boxes, has_geom = recognize_boxes(self.ocr, frame)
        self.last_boxes, self.last_text = boxes, text
        self.status.ocr_geometry = has_geom
        uia, uia_note = self.uia_probe()
        self.status.uia_note = uia_note
        extra = []
        if self.extra_parser is not None:
            try:
                extra = self.extra_parser(frame) or []
            except Exception as exc:
                self.on_event(f"external screen parser failed: {exc}")
        lay = discover_layout(frame, boxes, uia, ts, dpi_scale=self.dpi_scale, studio_version=self.studio_version,
                              uia_note=uia_note, anchors=self.anchors, prior=self.layout)
        for el in extra:
            if el.type not in lay.elements or el.confidence > lay.elements[el.type].confidence:
                lay.elements[el.type] = el
        from_cache = False
        if lay.status != "detected" and self.store is not None:
            cached = self.store.get(lay.signature)
            if cached is not None and cached.size == frame.size:
                ok, problems = validate_layout(cached, frame, boxes)
                if ok:
                    for k, el in cached.elements.items():
                        if k not in lay.elements:
                            lay.elements[k] = el
                            lay.elements[k].source = "cache"
                            from_cache = True
                    lay.notes.append("filled missing elements from the cached profile (validated against this frame)")
                else:
                    lay.notes.append("cached profile contradicted by fresh evidence; ignored: " + "; ".join(problems)[:100])
        if not has_geom:
            lay.notes.append("OCR backend gives no geometry: discovery limited to text presence")
        self.layout = lay
        if self.store is not None and lay.status in ("detected", "partly") and not from_cache:
            self.store.put(lay)
        self.status.state = lay.status
        self.status.layout_signature = lay.signature
        self.status.notes = list(lay.notes)
        self.status.from_cache = from_cache
        self.status.elements = {k: {"box": list(v.box), "confidence": v.confidence, "source": v.source, "detail": v.detail}
                                for k, v in lay.elements.items() if v.valid}
        self.status.transient = [{"type": t.type, "box": list(t.box), "confidence": t.confidence, "detail": t.detail} for t in lay.transient]
        found = ", ".join(sorted(k for k in lay.elements if lay.elements[k].valid))
        self.on_event(f"layout {lay.status} ({reason}; {self.status.last_ms:.0f} ms): {found or 'nothing'}")

    # ------------------------------------------------------------------ worker thread
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            self.run_pending()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="perception", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def busy(self) -> bool:
        return self._busy
