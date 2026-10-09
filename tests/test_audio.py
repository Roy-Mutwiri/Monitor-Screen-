"""Audio source resolution and real-time listening with fakes: sessions,
endpoints, loopback factory, meter reader, VAD, transcriber. No devices are
touched and nothing is recorded."""
from __future__ import annotations

import math

import numpy as np
import pytest

from conftest import FakeClock
from studio_monitor.audio.levels import AudioAnalyzer, AudioConfig, EnergyVad, make_vad
from studio_monitor.audio.resolver import (KIND_INPUT, KIND_LOOPBACK, KIND_NONE, KIND_SESSION, KIND_VISUAL, AudioEndpoint, AudioSession,
                                           AudioSourceResolver, process_loopback_supported)
from studio_monitor.audio.transcribe import ChunkedTranscriber, dedupe_overlap
from studio_monitor.audio.worker import AudioWorker

SR = 16000


def pcm(kind: str, seconds: float = 0.1, amp: float = 0.3, seed: int = 0, offset: float = 0.0) -> bytes:
    """``offset`` continues the waveform phase across consecutive chunks (seed doubles as chunk index)."""
    n = int(SR * seconds)
    t = (np.arange(n) + int((offset or seed * seconds) * SR)) / SR
    rng = np.random.RandomState(seed)
    if kind == "silence":
        x = np.zeros(n)
    elif kind == "music":                      # steady tones, no speech-like modulation
        x = amp * (np.sin(2 * math.pi * 220 * t) + 0.5 * np.sin(2 * math.pi * 330 * t)) / 1.5
    elif kind == "speech":                     # band-limited noise bursts with syllabic modulation
        env = 0.5 + 0.5 * np.sign(np.sin(2 * math.pi * 4 * t))
        x = amp * env * np.convolve(rng.randn(n), np.ones(8) / 8, mode="same")
    elif kind == "clipping":
        x = np.clip(2.0 * np.sin(2 * math.pi * 300 * t), -1, 1)
    else:
        raise ValueError(kind)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------- resolver

def make_resolver(sessions, endpoints, loopback=True, probe=None):
    return AudioSourceResolver(sessions=lambda: sessions, endpoints=lambda: endpoints, loopback_supported=loopback, loopback_probe=probe)


STUDIO = [AudioSession(100, "TikTok LIVE Studio.exe", True), AudioSession(555, "chrome.exe", True)]
ENDPOINTS = [AudioEndpoint("Realtek HD Audio 2nd output", False, True), AudioEndpoint("Microphone (USB Audio)", True, True),
             AudioEndpoint("Line In (Realtek)", True, True), AudioEndpoint("Headset Mic", True, False)]


def test_resolver_prefers_process_loopback_then_session_then_visual():
    r = make_resolver(STUDIO, ENDPOINTS, loopback=True, probe=lambda pid: True)
    b = r.resolve([100], [], "auto")
    assert b.kind == KIND_LOOPBACK and b.pid == 100 and b.samples and "may exclude mic" in b.label
    b = make_resolver(STUDIO, ENDPOINTS, loopback=True, probe=lambda pid: False).resolve([100], [], "auto")
    assert b.kind == KIND_SESSION and not b.samples and "level only" in b.label
    b = make_resolver([], ENDPOINTS, loopback=False).resolve([100], [], "auto")
    assert b.kind == KIND_VISUAL and "unsupported" in b.detail
    b = make_resolver(STUDIO, ENDPOINTS).resolve([100], [], "off")
    assert b.kind == KIND_NONE


def test_resolver_matches_explicit_ui_device_labels_and_reports_ambiguity():
    r = make_resolver([], ENDPOINTS, loopback=False)
    b = r.resolve([100], ["Microphone (USB Audio)"], "auto")
    assert b.kind == KIND_INPUT and b.endpoint.name == "Microphone (USB Audio)" and b.samples
    b = r.resolve([100], ["Microphone (USB Audio)", "Line In (Realtek)"], "auto")
    assert b.kind == KIND_NONE and set(b.choices) == {"Microphone (USB Audio)", "Line In (Realtek)"}   # one simple choice, no drawn region
    b = r.resolve([100], [], "input:Line In (Realtek)")
    assert b.kind == KIND_INPUT and b.endpoint.name == "Line In (Realtek)"
    b = r.resolve([100], [], "input:Nope")
    assert b.kind == KIND_NONE and "not present" in b.detail and b.choices
    assert process_loopback_supported("10.0.26200") and not process_loopback_supported("10.0.19041")


# ---------------------------------------------------------------- analyzer

def analyzer(clock, **over):
    cfg = AudioConfig(silence_seconds=3, recover_seconds=1, clipping_seconds=1, **over)
    a = AudioAnalyzer(cfg, SR, vad=EnergyVad(SR), mono=clock)
    a.bind(KIND_LOOPBACK, "Studio rendered audio", True)
    return a


def feed(a, clock, kind, seconds):
    conf, rec = [], []
    for i in range(int(seconds / 0.1)):
        c, r = a.feed_pcm(pcm(kind, 0.1, seed=i))
        conf += c; rec += r
        clock.advance(0.1)
    return [c[0] for c in conf], [r[0] for r in rec]


def test_silence_vs_music_vs_speech_vs_clipping():
    clock = FakeClock()
    a = analyzer(clock)
    conf, _ = feed(a, clock, "speech", 2)
    assert a.state.speech_ratio_10s > 0.3 and conf == []
    conf, _ = feed(a, clock, "music", 4)
    assert conf == [] and a.state.speech is False and "not silence" in a.state.note        # music without speech is not silence
    conf, _ = feed(a, clock, "silence", 4)
    assert conf == ["AUDIO_SILENCE"] and a.state.silent_seconds >= 3
    conf, rec = feed(a, clock, "music", 2)
    assert rec == ["AUDIO_SILENCE"]                                                          # recovery from silence
    conf, _ = feed(a, clock, "clipping", 2)
    assert "AUDIO_CLIPPING" in conf and a.state.clipping


def test_unavailable_source_never_reports_silence():
    clock = FakeClock()
    a = analyzer(clock)
    a.unavailable("capture error")
    for _ in range(60):
        clock.advance(1); a.unavailable("capture error")
    assert not a.silence.confirmed and a.state.available is False and "capture error" in a.state.note


def test_level_only_source_has_no_speech_claim():
    clock = FakeClock()
    cfg = AudioConfig(silence_seconds=2, recover_seconds=1)
    a = AudioAnalyzer(cfg, SR, vad=EnergyVad(SR), mono=clock)
    a.bind(KIND_SESSION, "Studio audio session level meter", False)
    for _ in range(25):
        a.feed_level(0.0); clock.advance(0.1)
    assert a.silence.confirmed and a.state.speech is False and "level-only" in a.state.note
    c, r = [], []
    for _ in range(15):
        _c, _r = a.feed_level(0.4); c += _c; r += _r; clock.advance(0.1)
    assert [x[0] for x in r] == ["AUDIO_SILENCE"]


def test_vad_selection_falls_back_cleanly():
    v = make_vad("energy")
    assert v.name == "energy"
    speech_hits = [v.is_speech(pcm("speech", 0.1, seed=i)) for i in range(10)]
    assert any(speech_hits[5:]) and not v.is_speech(pcm("silence", 0.1))
    auto = make_vad("auto")
    assert auto.name in ("webrtcvad", "energy")


# ---------------------------------------------------------------- transcription (fake backend)

class FakeWhisper:
    def __init__(self):
        self.calls = 0

    def transcribe(self, pcm16, sample_rate):
        self.calls += 1
        n = len(pcm16) // 2
        return [{"text": f"hello world chunk {self.calls}", "start": 0, "end": n / sample_rate, "avg_logprob": -0.3, "no_speech_prob": 0.1},
                {"text": "Thank you.", "start": 0, "end": 1, "avg_logprob": -1.5, "no_speech_prob": 0.9}]


def test_transcriber_is_vad_gated_chunked_and_deduped():
    clock = FakeClock()
    segs = []
    t = ChunkedTranscriber(FakeWhisper(), SR, chunk_seconds=1.0, overlap_seconds=0.2, mono=clock, on_segment=segs.append, inline=True)
    for _ in range(12):                       # 1.2 s of silence: dropped, never transcribed
        t.feed(pcm("silence", 0.1), False); clock.advance(0.1)
    assert t.dropped_silent == 1 and t.backend.calls == 0
    for _ in range(12):
        t.feed(pcm("speech", 0.1), True); clock.advance(0.1)
    assert t.backend.calls == 1 and segs and segs[0].text.startswith("hello world chunk") and "Thank you" not in segs[0].text
    assert segs[0].latency_ms >= 0
    assert dedupe_overlap("we are going live now", "live now with the chart") == "with the chart"
    assert dedupe_overlap("", "the the the chart chart") == "the chart"


# ---------------------------------------------------------------- worker

class FakeCapture:
    instances = []

    def __init__(self, pid, on_pcm):
        self.pid, self.on_pcm, self.error, self.started = pid, on_pcm, "", False
        FakeCapture.instances.append(self)

    def start(self): self.started = True
    def stop(self): self.started = False
    def push(self, kind): self.on_pcm(pcm(kind, 0.1))


def test_worker_binds_loopback_and_reports_conditions_without_touching_routing():
    clock = FakeClock()
    FakeCapture.instances.clear()
    events = []
    r = make_resolver(STUDIO, ENDPOINTS, loopback=True)
    cfg = AudioConfig(silence_seconds=1, recover_seconds=0.5)
    w = AudioWorker(r, AudioAnalyzer(cfg, SR, vad=EnergyVad(SR), mono=clock), loopback_factory=FakeCapture, on_event=events.append, mono=clock)
    w.set_context([100], [])
    b = w.rebind(force=True)
    assert b.kind == KIND_LOOPBACK and FakeCapture.instances[0].started and "audio source" in events[-1]
    cap = FakeCapture.instances[0]
    for _ in range(15):
        cap.push("silence"); w.tick(); clock.advance(0.1)
    c, rec = w.drain()
    assert [x[0] for x in c] == ["AUDIO_SILENCE"]
    st = w.status()
    assert st["binding"]["kind"] == KIND_LOOPBACK and st["available"] and st["problems"] == ["AUDIO_SILENCE"]
    cap.error = "device invalidated"
    w.tick()
    assert w.status()["available"] is False
    w.stop()
    assert cap.started is False


def test_worker_session_meter_and_fallback_when_loopback_fails():
    clock = FakeClock()
    events = []
    r = make_resolver(STUDIO, ENDPOINTS, loopback=True)
    def bad_factory(pid, cb):
        raise OSError("activation refused")
    levels = iter([0.0] * 30 + [0.5] * 10)
    w = AudioWorker(r, AudioAnalyzer(AudioConfig(silence_seconds=1, recover_seconds=0.5), SR, vad=EnergyVad(SR), mono=clock),
                    loopback_factory=bad_factory, meter_reader=lambda pid: next(levels, None), on_event=events.append, mono=clock)
    w.set_context([100], [])
    assert w.rebind(force=True).kind == KIND_SESSION and any("falling back" in e for e in events)
    for _ in range(40):
        w.tick(); clock.advance(0.1)
    c, rec = w.drain()
    assert [x[0] for x in c] == ["AUDIO_SILENCE"] and [x[0] for x in rec] == ["AUDIO_SILENCE"]
    assert "level-only" in w.status()["note"] or w.status()["speech"] is False
