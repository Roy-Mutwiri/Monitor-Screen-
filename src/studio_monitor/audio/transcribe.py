"""Optional local transcription (faster-whisper), VAD-gated and chunked.

Off by default; the model is installed by the operator in the external
parser environment and runs on a worker thread outside the capture loop.
Transcripts are observed content: shown locally, never uploaded or executed,
and never written to disk unless the operator enables the local log."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import numpy as np


class Transcriber(Protocol):
    def transcribe(self, pcm16: bytes, sample_rate: int) -> list[dict]: ...   # [{"text", "start", "end", "avg_logprob", "no_speech_prob"}]


@dataclass
class TranscriptSegment:
    text: str
    t0: float
    t1: float
    latency_ms: float


class FasterWhisperTranscriber:
    name = "faster-whisper"

    def __init__(self, model: str = "tiny", device: str = "auto", compute_type: str = "int8") -> None:
        from faster_whisper import WhisperModel
        self.model = WhisperModel(model, device=device, compute_type=compute_type)

    @staticmethod
    def available() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def transcribe(self, pcm16: bytes, sample_rate: int) -> list[dict]:
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segs, _info = self.model.transcribe(audio, language=None, vad_filter=True, beam_size=1, condition_on_previous_text=False)
        return [{"text": s.text, "start": s.start, "end": s.end, "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob} for s in segs]


_HALLUCINATION = re.compile(r"^(?:\s*(?:thank you|thanks for watching|subscribe|you)\.?\s*)+$", re.I)


def dedupe_overlap(previous: str, text: str) -> str:
    """Remove the prefix of ``text`` that repeats the tail of ``previous`` (overlapping chunks)."""
    p = previous.split(); t = text.split()
    for k in range(min(len(p), len(t), 12), 0, -1):
        if [w.lower().strip(".,") for w in p[-k:]] == [w.lower().strip(".,") for w in t[:k]]:
            return " ".join(t[k:])
    # collapse immediate word repeats ("the the the")
    out = []
    for w in t:
        if not out or out[-1].lower() != w.lower():
            out.append(w)
    return " ".join(out)


class ChunkedTranscriber:
    """Accumulates VAD-positive audio into chunks (default 6 s with 1 s overlap), transcribes on a worker thread
    and suppresses hallucinated output during silence."""

    def __init__(self, backend: Transcriber, sample_rate: int = 16000, chunk_seconds: float = 6.0, overlap_seconds: float = 1.0,
                 min_speech_ratio: float = 0.2, mono: Callable[[], float] = time.monotonic,
                 on_segment: Optional[Callable[[TranscriptSegment], None]] = None, inline: bool = False) -> None:
        self.backend, self.sr = backend, sample_rate
        self.chunk_bytes = int(chunk_seconds * sample_rate) * 2
        self.overlap_bytes = int(overlap_seconds * sample_rate) * 2
        self.min_speech_ratio = min_speech_ratio
        self.mono = mono
        self.on_segment = on_segment or (lambda s: None)
        self.inline = inline
        self._buf = bytearray()
        self._speech_flags: deque[bool] = deque()
        self._queue: deque[tuple[bytes, float]] = deque(maxlen=2)      # latest chunks only: no backlog
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.previous_text = ""
        self.segments: list[TranscriptSegment] = []
        self.dropped_silent = 0
        self.last_latency_ms = 0.0

    def feed(self, pcm16: bytes, speech: bool) -> None:
        self._buf += pcm16
        self._speech_flags.append(speech)
        if len(self._buf) >= self.chunk_bytes:
            ratio = sum(self._speech_flags) / max(1, len(self._speech_flags))
            chunk = bytes(self._buf)
            self._buf = bytearray(self._buf[-self.overlap_bytes:])
            self._speech_flags.clear()
            if ratio < self.min_speech_ratio:
                self.dropped_silent += 1                               # VAD gate: never transcribe silence
                return
            with self._lock:
                self._queue.append((chunk, self.mono()))
            if self.inline:
                self.process_pending()

    def process_pending(self) -> int:
        n = 0
        while True:
            with self._lock:
                item = self._queue.popleft() if self._queue else None
            if item is None:
                return n
            chunk, t_in = item
            try:
                segs = self.backend.transcribe(chunk, self.sr)
            except Exception:
                return n
            text = " ".join(s["text"].strip() for s in segs
                            if s["text"].strip() and float(s.get("no_speech_prob", 0)) < 0.6 and float(s.get("avg_logprob", 0)) > -1.2)
            text = dedupe_overlap(self.previous_text, text)
            if text and not _HALLUCINATION.match(text):
                lat = (self.mono() - t_in) * 1000
                self.last_latency_ms = lat
                seg = TranscriptSegment(text, t_in, self.mono(), lat)
                self.segments.append(seg)
                self.segments = self.segments[-200:]
                self.previous_text = (self.previous_text + " " + text)[-400:]
                self.on_segment(seg)
            n += 1

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.process_pending():
                self._stop.wait(0.2)

    def start(self) -> None:
        if self._thread is None and not self.inline:
            self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
