"""Bounded local audio analysis: RMS/peak per short buffer, sustained
silence with hysteresis, clipping, speech activity (webrtcvad when
installed, otherwise an energy/zero-crossing VAD), recovery from silence.

Rules: music without speech is *not* silence; "no speech" never raises an
audio-loss alert; measurements are only valid while the source is bound.
Buffers are short (<= 3 s) and never written to disk."""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from ..detectors.text_rules import SustainedCondition


@dataclass
class AudioReadingState:
    available: bool = False
    source_kind: str = "unavailable"
    source_label: str = ""
    rms_dbfs: float = -100.0
    peak: float = 0.0
    speech: bool = False
    speech_ratio_10s: float = 0.0
    silent_seconds: float = 0.0
    clipping: bool = False
    problems: list[str] = field(default_factory=list)
    note: str = ""
    samples: bool = False

    def to_dict(self) -> dict:
        return {"available": self.available, "source": self.source_kind, "label": self.source_label, "rms_dbfs": round(self.rms_dbfs, 1),
                "peak": round(self.peak, 3), "speech": self.speech, "speech_ratio_10s": round(self.speech_ratio_10s, 2),
                "silent_seconds": round(self.silent_seconds, 1), "clipping": self.clipping, "problems": list(self.problems),
                "note": self.note, "samples": self.samples}


def dbfs(rms: float) -> float:
    return 20 * math.log10(rms) if rms > 1e-6 else -100.0


class EnergyVad:
    """Dependency-free fallback: speech = band-limited energy above an adaptive floor with a plausible zero-crossing rate."""
    name = "energy"

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sr = sample_rate
        self.floor = 1e-4
        self._env: deque = deque(maxlen=50)      # ~0.5 s of 10 ms sub-frame RMS values

    def is_speech(self, pcm16: bytes, sample_rate: Optional[int] = None) -> bool:
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size < 160:
            return False
        rms = float(np.sqrt(np.mean(x * x)))
        self.floor = min(self.floor * 1.02 + 1e-6, max(self.floor * 0.98, rms * 0.3)) if rms < self.floor * 3 else self.floor
        zcr = float(np.mean(np.abs(np.diff(np.sign(x))) > 0))
        # syllabic modulation: speech energy varies a lot over ~0.5 s of 10 ms sub-frames, steady tones much less
        n = max(1, len(x) // 10)
        sub = x[: n * 10].reshape(10, n) if len(x) >= 10 else x.reshape(1, -1)
        for v in np.sqrt(np.mean(sub * sub, axis=1)):
            self._env.append(float(v) + 1e-6)
        env = np.asarray(self._env)
        modulation = float(env.std() / env.mean()) if len(env) >= 20 else 1.0
        return rms > max(0.01, self.floor * 4) and 0.02 < zcr < 0.35 and modulation > 0.25


class WebRtcVad:
    name = "webrtcvad"

    def __init__(self, aggressiveness: int = 2) -> None:
        import importlib
        webrtcvad = importlib.import_module("webrtcvad")      # dynamic: keeps PyInstaller's broken contrib hook out of the build
        self._vad = webrtcvad.Vad(aggressiveness)

    @staticmethod
    def available() -> bool:
        try:
            import importlib
            importlib.import_module("webrtcvad")
            return True
        except Exception:
            return False

    def is_speech(self, pcm16: bytes, sample_rate: int = 16000) -> bool:
        # webrtcvad accepts 10/20/30 ms frames at 8/16/32/48 kHz
        frame = int(sample_rate * 0.03) * 2
        if len(pcm16) < frame:
            return False
        hits = 0; n = 0
        for i in range(0, len(pcm16) - frame + 1, frame):
            n += 1
            try:
                if self._vad.is_speech(pcm16[i:i + frame], sample_rate):
                    hits += 1
            except Exception:
                return False
        return n > 0 and hits / n >= 0.3


def make_vad(preference: str = "auto"):
    if preference in ("auto", "webrtcvad") and WebRtcVad.available():
        return WebRtcVad()
    return EnergyVad()


@dataclass
class AudioConfig:
    silence_seconds: float = 30.0
    silence_dbfs: float = -55.0
    clipping_peak: float = 0.985
    clipping_seconds: float = 5.0
    recover_seconds: float = 5.0
    buffer_seconds: float = 3.0
    vad: str = "auto"


class AudioAnalyzer:
    """Feed short PCM16 mono buffers (or level-only readings) and read conditions. Clocks injectable."""

    def __init__(self, cfg: AudioConfig, sample_rate: int = 16000, vad=None, mono: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.sr = sample_rate
        self.vad = vad or make_vad(cfg.vad)
        self.mono = mono
        self.state = AudioReadingState()
        self.silence = SustainedCondition("AUDIO_SILENCE", cfg.silence_seconds, cfg.recover_seconds, max_gap_seconds=10)
        self.clip = SustainedCondition("AUDIO_CLIPPING", cfg.clipping_seconds, cfg.recover_seconds, max_gap_seconds=10)
        self._speech_hist: deque[tuple[float, bool]] = deque()
        self._silent_since: Optional[float] = None
        self._buffer = bytearray()

    # ------------------------------------------------------------------
    def bind(self, kind: str, label: str, samples: bool) -> None:
        self.state = AudioReadingState(available=kind not in ("unavailable",), source_kind=kind, source_label=label, samples=samples)
        self.silence.reset(); self.clip.reset()
        self._silent_since = None
        self._speech_hist.clear()
        self._buffer.clear()

    def unavailable(self, note: str) -> list[tuple[str, str]]:
        self.state.available, self.state.note = False, note
        out = []
        for c in (self.silence, self.clip):
            c.update(False, False, self.mono())
        return out

    def feed_level(self, peak: float, rms: Optional[float] = None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Level-only source (session meter): no VAD possible."""
        now = self.mono()
        rms = rms if rms is not None else peak / math.sqrt(2)
        self.state.available, self.state.peak, self.state.rms_dbfs = True, float(peak), dbfs(rms)
        self.state.speech = False
        return self._evaluate(now, speech=None)

    def feed_pcm(self, pcm16: bytes) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        now = self.mono()
        self._buffer += pcm16
        max_bytes = int(self.cfg.buffer_seconds * self.sr) * 2
        if len(self._buffer) > max_bytes:
            del self._buffer[: len(self._buffer) - max_bytes]
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size == 0:
            return [], []
        rms = float(np.sqrt(np.mean(x * x)))
        peak = float(np.max(np.abs(x)))
        speech = bool(self.vad.is_speech(pcm16, self.sr))
        self.state.available, self.state.peak, self.state.rms_dbfs, self.state.speech = True, peak, dbfs(rms), speech
        self._speech_hist.append((now, speech))
        while self._speech_hist and now - self._speech_hist[0][0] > 10:
            self._speech_hist.popleft()
        self.state.speech_ratio_10s = sum(1 for _t, s in self._speech_hist if s) / max(1, len(self._speech_hist))
        return self._evaluate(now, speech)

    def _evaluate(self, now: float, speech: Optional[bool]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        silent = self.state.rms_dbfs <= self.cfg.silence_dbfs and self.state.peak < 0.01
        # music / any sound counts as "not silent" regardless of VAD; VAD never creates a silence condition
        if silent:
            self._silent_since = self._silent_since or now
        else:
            self._silent_since = None
        self.state.silent_seconds = (now - self._silent_since) if self._silent_since else 0.0
        self.state.clipping = self.state.peak >= self.cfg.clipping_peak
        confirmed, recovered = [], []
        for cond, active, text_on, text_off in (
            (self.silence, silent, "No audio from {src} for {d:.0f} s (RMS {rms:.0f} dBFS).", "Audio level is back."),
            (self.clip, self.state.clipping, "Audio is clipping (peak >= {thr:.2f}) for {d:.0f} s.", "Audio no longer clipping."),
        ):
            res = cond.update(active, True, now, f"{self.state.rms_dbfs:.0f} dBFS")
            if res == "confirmed":
                confirmed.append((cond.name, text_on.format(src=self.state.source_label or "the bound source", thr=self.cfg.clipping_peak,
                                                            rms=self.state.rms_dbfs, d=cond.duration(now) or cond.sustain_seconds)))
            elif res == "recovered":
                recovered.append((cond.name, text_off))
        self.state.problems = [c.name for c in (self.silence, self.clip) if c.confirmed]
        if speech is None:
            self.state.note = "level-only source: speech activity not measured"
        elif not speech and not silent:
            self.state.note = "sound without detected speech (music/ambience?) — not silence"
        else:
            self.state.note = ""
        return confirmed, recovered
