"""Detector suite: evaluates connection / source / preview / presenter / audio
conditions on each fresh frame and emits confirmed / recovered transitions.

Evaluation only happens when the broadcast state is confirmed LIVE, the
suite is enabled and not suppressed (maintenance, profile-menu lookup, scene
transition grace). Invalid frames pause every clock (UNKNOWN).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from ..regions import Region
from .audio import PROFILES, AudioReading, read_meter
from .presenter import FaceDetectorBackend, PresenterAnalyzer, PresenterObservation, black_level
from .text_rules import ConnectionRules, SustainedCondition


@dataclass
class DetectorsConfig:
    enabled: bool = True
    presenter_enabled: bool = True
    presenter_expected: bool = True          # profile: presenter-expected | presenter-not-expected
    face_absent_seconds: float = 30.0
    motion_low_seconds: float = 60.0
    motion_threshold: float = 0.035
    frozen_seconds: float = 20.0
    black_preview_seconds: float = 20.0
    black_luma: float = 12.0
    source_sustain_seconds: float = 10.0
    connection_sustain_seconds: float = 10.0
    connection_recover_seconds: float = 20.0
    recover_seconds: float = 15.0
    scene_change_grace_seconds: float = 10.0
    audio_enabled: bool = False              # needs a calibrated meter region
    audio_silence_seconds: float = 30.0
    audio_level_threshold: float = 0.05
    audio_profile: str = "mixed"
    rules_file: str = ""


@dataclass
class Condition:
    name: str
    state: str                 # OK | PROBLEM | UNKNOWN | DISABLED
    since: Optional[float] = None
    detail: str = ""


@dataclass
class SuiteOutput:
    confirmed: list[tuple[str, str]] = field(default_factory=list)   # (condition, detail)
    recovered: list[tuple[str, str]] = field(default_factory=list)
    conditions: dict[str, Condition] = field(default_factory=dict)
    presenter: Optional[PresenterObservation] = None
    audio: Optional[AudioReading] = None


CONDITIONS = ("RECONNECTING", "SOURCE_MISSING", "BLACK_PREVIEW", "FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN", "AUDIO_SILENCE")
CATEGORY_OF = {"RECONNECTING": "connection", "SOURCE_MISSING": "source", "BLACK_PREVIEW": "source",
               "FACE_ABSENT": "face", "FACE_MOTION_LOW": "face", "PREVIEW_FROZEN": "face", "AUDIO_SILENCE": "audio"}

WORDING = {
    "FACE_ABSENT": "No face detected in the configured presenter region for {d:.0f} seconds.",
    "FACE_MOTION_LOW": "Face remains visible with very little detected movement for {d:.0f} seconds (presenter appears unusually still).",
    "PREVIEW_FROZEN": "The presenter region has not changed for {d:.0f} seconds while the rest of Studio kept updating (preview may be frozen).",
    "BLACK_PREVIEW": "The preview region has been black for {d:.0f} seconds.",
    "SOURCE_MISSING": "Studio shows a missing/unavailable source message ({ev}) for {d:.0f} seconds.",
    "RECONNECTING": "Studio shows a connection problem ({ev}) for {d:.0f} seconds; broadcast state is not changed by this.",
    "AUDIO_SILENCE": "Studio's audio meter shows no activity for {d:.0f} seconds (signal measured: on-screen meter).",
}
RECOVERY_WORDING = {
    "FACE_ABSENT": "A face is detected again in the presenter region.",
    "FACE_MOTION_LOW": "Movement detected again in the presenter region.",
    "PREVIEW_FROZEN": "The presenter region is updating again.",
    "BLACK_PREVIEW": "The preview region is no longer black.",
    "SOURCE_MISSING": "The missing-source message is no longer visible.",
    "RECONNECTING": "The connection problem message is no longer visible.",
    "AUDIO_SILENCE": "Audio meter activity resumed.",
}


class DetectorSuite:
    SCENE_CHANGE_DIFF = 0.30   # mean absolute luminance change (0..1) of the whole frame

    def __init__(self, cfg: DetectorsConfig, rules: ConnectionRules, face_backend: Optional[FaceDetectorBackend],
                 clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.rules = rules
        self.clock = clock
        self.mono = mono
        self.analyzer = PresenterAnalyzer(face_backend, cfg.motion_threshold) if face_backend else None
        self.face_backend_name = getattr(face_backend, "name", "") if face_backend else ""
        c = cfg
        self.cond = {
            "RECONNECTING": SustainedCondition("RECONNECTING", c.connection_sustain_seconds, c.connection_recover_seconds),
            "SOURCE_MISSING": SustainedCondition("SOURCE_MISSING", c.source_sustain_seconds, c.recover_seconds),
            "BLACK_PREVIEW": SustainedCondition("BLACK_PREVIEW", c.black_preview_seconds, c.recover_seconds),
            "FACE_ABSENT": SustainedCondition("FACE_ABSENT", c.face_absent_seconds, c.recover_seconds),
            "FACE_MOTION_LOW": SustainedCondition("FACE_MOTION_LOW", c.motion_low_seconds, c.recover_seconds),
            "PREVIEW_FROZEN": SustainedCondition("PREVIEW_FROZEN", c.frozen_seconds, c.recover_seconds),
            "AUDIO_SILENCE": SustainedCondition("AUDIO_SILENCE", c.audio_silence_seconds, c.recover_seconds),
        }
        self._grace_until = 0.0
        self.presenter_state = "unavailable"
        self._last_frame: Optional[np.ndarray] = None
        self.scene_changes = 0
        self.reconnect_episodes = 0
        self.last_reconnect_end: Optional[float] = None

    # ------------------------------------------------------------------
    def suspend(self, seconds: float) -> None:
        """Bounded grace (scene transition, profile-menu lookup, break)."""
        self._grace_until = max(self._grace_until, self.mono() + seconds)

    def reset(self) -> None:
        for c in self.cond.values():
            c.reset()
        if self.analyzer:
            self.analyzer.reset()
        self._last_frame = None

    @property
    def in_grace(self) -> bool:
        return self.mono() < self._grace_until

    def classify_text(self, text: str) -> dict:
        return self.rules.classify(text)

    def evaluate(self, frame: Optional[Image.Image], fresh: bool, live: bool, text: str, regions: list[Region],
                 suppressed: bool = False) -> SuiteOutput:
        out = SuiteOutput()
        now = self.mono()
        active_eval = self.cfg.enabled and live and not suppressed and now >= self._grace_until
        face_region = next((r for r in regions if r.kind == "face"), None)
        audio_region = next((r for r in regions if r.kind == "audio"), None)
        valid_frame = frame is not None and fresh and active_eval

        # scene-transition grace: a very large whole-frame change (scene switch, layout change) pauses
        # presenter evaluation briefly so the new layout is not misread as "face absent".
        if frame is not None and fresh:
            small = np.asarray(frame.convert("L").resize((48, 27), Image.BILINEAR), dtype=np.float32) / 255.0
            if self._last_frame is not None and float(np.mean(np.abs(small - self._last_frame))) > self.SCENE_CHANGE_DIFF:
                self._grace_until = max(self._grace_until, now + self.cfg.scene_change_grace_seconds)
                self.scene_changes += 1
                if self.analyzer is not None:
                    self.analyzer.reset()
                for k in ("FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN", "BLACK_PREVIEW"):
                    self.cond[k].restart()
                valid_frame = False
            self._last_frame = small

        # text conditions
        cls = self.rules.classify(text) if (valid_frame and text is not None) else {"reconnecting": None, "ended": None, "source_missing": None}
        self._apply(out, "RECONNECTING", bool(cls["reconnecting"]), valid_frame, now, cls["reconnecting"] or "")
        self._apply(out, "SOURCE_MISSING", bool(cls["source_missing"]), valid_frame, now, cls["source_missing"] or "")

        # presenter / preview region
        if face_region is not None and frame is not None:
            box = face_region.to_box(*frame.size)
            if self.cfg.presenter_enabled and self.analyzer is not None:
                obs = self.analyzer.observe(frame, box, valid_frame)
                out.presenter = obs
                if obs.valid:
                    luma = black_level(frame, box)
                    self._apply(out, "BLACK_PREVIEW", luma < self.cfg.black_luma, True, now, f"luma {luma:.0f}")
                    expected = self.cfg.presenter_expected
                    if obs.ambiguous:
                        # several comparable faces or a tiny face: report the uncertainty, never pick one silently
                        self._apply(out, "FACE_ABSENT", False, False, now)
                        self._apply(out, "FACE_MOTION_LOW", False, False, now)
                        self.cond["FACE_ABSENT"].last_evidence = obs.ambiguous
                        self.presenter_state = "unclear: " + obs.ambiguous
                    else:
                        absent = obs.face is None and expected and luma >= self.cfg.black_luma
                        self._apply(out, "FACE_ABSENT", absent, True, now, "no face")
                        still = obs.face is not None and obs.motion < self.cfg.motion_threshold
                        self._apply(out, "FACE_MOTION_LOW", still, True, now, f"motion {obs.motion:.3f}")
                        self.presenter_state = "absent" if obs.face is None else "detected"
                    # a black source repeats identical buffers too; that is BLACK_PREVIEW, not a frozen preview
                    frozen = (not obs.region_changed) and obs.frame_changed_elsewhere and luma >= self.cfg.black_luma
                    self._apply(out, "PREVIEW_FROZEN", frozen, True, now, "region unchanged while frame changed")
                else:
                    for k in ("BLACK_PREVIEW", "FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN"):
                        self._apply(out, k, False, False, now)
            else:
                for k in ("FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN"):
                    self.cond[k].unknown = True
                if valid_frame:
                    luma = black_level(frame, box)
                    self._apply(out, "BLACK_PREVIEW", luma < self.cfg.black_luma, True, now, f"luma {luma:.0f}")
        else:
            for k in ("BLACK_PREVIEW", "FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN"):
                self.cond[k].unknown = True
            self.presenter_state = "unavailable"

        # audio meter
        if self.cfg.audio_enabled and audio_region is not None and frame is not None and valid_frame:
            reading = read_meter(frame, audio_region.to_box(*frame.size))
            out.audio = reading
            self._apply(out, "AUDIO_SILENCE", reading.valid and reading.level < self.cfg.audio_level_threshold, reading.valid, now,
                        f"level {reading.level:.2f}" if reading.valid else reading.note)
        else:
            self.cond["AUDIO_SILENCE"].unknown = True

        out.conditions = self.snapshot()
        return out

    def _apply(self, out: SuiteOutput, name: str, active: bool, valid: bool, now: float, evidence: str = "") -> None:
        c = self.cond[name]
        was_confirmed = c.confirmed
        res = c.update(active, valid, now, evidence)
        if res == "confirmed":
            out.confirmed.append((name, WORDING[name].format(d=c.duration(now) or c.sustain_seconds, ev=c.last_evidence)))
            if name == "RECONNECTING":
                self.reconnect_episodes += 1
        elif res == "recovered" and was_confirmed:
            out.recovered.append((name, RECOVERY_WORDING[name]))
            if name == "RECONNECTING":
                self.last_reconnect_end = now

    def snapshot(self) -> dict[str, Condition]:
        snap = {}
        for name, c in self.cond.items():
            if name in ("FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN") and (not self.cfg.presenter_enabled or self.analyzer is None):
                snap[name] = Condition(name, "DISABLED", detail="presenter monitoring off or face model unavailable")
            elif name == "AUDIO_SILENCE" and not self.cfg.audio_enabled:
                snap[name] = Condition(name, "DISABLED", detail="audio meter not calibrated")
            elif c.unknown:
                snap[name] = Condition(name, "UNKNOWN", detail="no fresh valid frame / not evaluated")
            elif c.confirmed:
                snap[name] = Condition(name, "PROBLEM", c.episode_started, c.last_evidence)
            else:
                snap[name] = Condition(name, "OK", detail=c.last_evidence)
        return snap
