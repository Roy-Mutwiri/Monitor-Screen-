"""AudioWorker: binds the resolved source, pulls short buffers (process
loopback samples or session-meter levels), runs the analyzer and exposes a
status snapshot plus confirmed/recovered conditions for the monitor.
Bounded, independent of capture and perception; inline mode for tests."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

from .levels import AudioAnalyzer, AudioConfig
from .resolver import KIND_INPUT, KIND_LOOPBACK, KIND_NONE, KIND_SESSION, KIND_VISUAL, AudioBinding, AudioSourceResolver

log = logging.getLogger(__name__)


class AudioWorker:
    def __init__(self, resolver: AudioSourceResolver, analyzer: AudioAnalyzer, *, preference: str = "auto",
                 loopback_factory: Optional[Callable[[int, Callable[[bytes], None]], object]] = None,
                 meter_reader: Optional[Callable[[int], Optional[float]]] = None,
                 transcriber=None, on_event: Optional[Callable[[str], None]] = None, mono: Callable[[], float] = time.monotonic,
                 rebind_interval: float = 30.0) -> None:
        self.resolver, self.analyzer = resolver, analyzer
        self.preference = preference
        self.loopback_factory = loopback_factory
        self.meter_reader = meter_reader
        self.transcriber = transcriber
        self.on_event = on_event or (lambda m: log.info(m))
        self.mono = mono
        self.rebind_interval = rebind_interval
        self.binding: AudioBinding = AudioBinding(KIND_NONE, "not started", 0.0)
        self._capture = None
        self._pcm: deque[bytes] = deque(maxlen=50)          # ≤ 5 s at 100 ms chunks; never persisted
        self._lock = threading.Lock()
        self._last_bind = -1e9
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.confirmed: list[tuple[str, str]] = []
        self.recovered: list[tuple[str, str]] = []
        self.studio_pids: list[int] = []
        self.ui_labels: list[str] = []
        self._last_level = 0.0

    # ------------------------------------------------------------------
    def set_context(self, studio_pids: list[int], ui_labels: list[str]) -> None:
        self.studio_pids, self.ui_labels = list(studio_pids), list(ui_labels)

    def rebind(self, force: bool = False) -> AudioBinding:
        now = self.mono()
        if not force and now - self._last_bind < self.rebind_interval:
            return self.binding
        self._last_bind = now
        b = self.resolver.resolve(self.studio_pids, self.ui_labels, self.preference)
        if b.kind != self.binding.kind or b.pid != self.binding.pid or (b.endpoint and self.binding.endpoint and b.endpoint.name != self.binding.endpoint.name):
            self._teardown()
            self.binding = b
            self.analyzer.bind(b.kind, b.label, b.samples)
            if b.kind == KIND_LOOPBACK and self.loopback_factory is not None:
                try:
                    self._capture = self.loopback_factory(b.pid, self._on_pcm)
                    self._capture.start()
                except Exception as exc:
                    self.on_event(f"audio: process loopback failed ({exc}); falling back")
                    self.binding = AudioBinding(KIND_SESSION, "Studio audio session level meter (loopback failed)", 0.5, str(exc)[:100], pid=b.pid)
                    self.analyzer.bind(self.binding.kind, self.binding.label, False)
            self.on_event(f"audio source: {self.binding.label} ({self.binding.detail})" + (f"; choices: {', '.join(b.choices)}" if b.choices else ""))
        else:
            self.binding = b
        return self.binding

    def _teardown(self) -> None:
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None
        with self._lock:
            self._pcm.clear()

    def _on_pcm(self, pcm: bytes) -> None:
        with self._lock:
            self._pcm.append(pcm)

    # ------------------------------------------------------------------
    def tick(self) -> None:
        """Consume pending buffers / read the meter once. Returns nothing; conditions accumulate in confirmed/recovered."""
        b = self.binding
        if b.kind in (KIND_LOOPBACK, KIND_INPUT):
            with self._lock:
                chunks = list(self._pcm); self._pcm.clear()
            if self._capture is not None and getattr(self._capture, "error", ""):
                self.analyzer.unavailable(f"capture error: {self._capture.error}")
                return
            for pcm in chunks:
                c, r = self.analyzer.feed_pcm(pcm)
                self.confirmed += c; self.recovered += r
                if self.transcriber is not None:
                    self.transcriber.feed(pcm, self.analyzer.state.speech)
        elif b.kind == KIND_SESSION and self.meter_reader is not None:
            level = self.meter_reader(b.pid)
            if level is None:
                self.analyzer.unavailable("session meter unreadable")
                return
            c, r = self.analyzer.feed_level(level)
            self.confirmed += c; self.recovered += r
        else:
            self.analyzer.unavailable(b.detail or b.label)

    def drain(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        c, r = self.confirmed, self.recovered
        self.confirmed, self.recovered = [], []
        return c, r

    def status(self) -> dict:
        d = self.analyzer.state.to_dict()
        d["binding"] = self.binding.to_dict()
        if self.transcriber is not None:
            d["transcript_latency_ms"] = round(getattr(self.transcriber, "last_latency_ms", 0.0), 0)
            d["transcript_last"] = (self.transcriber.segments[-1].text if self.transcriber.segments else "")
        return d

    # ------------------------------------------------------------------ thread
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.rebind()
                self.tick()
            except Exception as exc:  # pragma: no cover
                log.exception("audio worker: %s", exc)
            self._stop.wait(0.2)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="studio-audio", daemon=True)
            self._thread.start()
            if self.transcriber is not None:
                self.transcriber.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._teardown()
        if self.transcriber is not None:
            self.transcriber.stop()


def session_peak_reader(pid: int) -> Optional[float]:
    """Peak (0..1) of the audio session of ``pid`` via pycaw's IAudioMeterInformation; None when unreadable."""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
        for s in AudioUtilities.GetAllSessions():
            if int(s.ProcessId or 0) == pid:
                return float(s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
    except Exception:
        return None
    return None
