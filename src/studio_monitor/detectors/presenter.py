"""Presenter / face region detector: presence, in-face motion, frozen preview.

Only the configured presenter region of the *Studio* frame is analysed. A
CPU face detector (YuNet via OpenCV, Apache-2.0 model with pinned checksum)
returns boxes and five landmarks; motion is measured inside the face box so
a moving background does not count, and small compression noise is below
the motion threshold. No embeddings or identity are computed.

Frozen preview needs temporal evidence: identical presenter-region pixels
across several *fresh* frames spanning the freeze interval while the rest of
the frame changed. A static WGC stream (no new frames) is UNKNOWN, never
frozen. Capture failure is UNKNOWN, never "face absent".
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Optional, Protocol

import numpy as np
from PIL import Image, ImageFilter

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"


@dataclass
class Face:
    box: tuple[int, int, int, int]            # x, y, w, h in region pixels
    landmarks: list[tuple[float, float]]      # 5 points (eyes, nose, mouth corners)
    score: float


class FaceDetectorBackend(Protocol):
    name: str

    def detect(self, image: Image.Image) -> list[Face]: ...


def model_path() -> str:
    import sys
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "models", MODEL_NAME)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "models", MODEL_NAME)


def verify_model(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest() == MODEL_SHA256
    except OSError:
        return False


class YuNetDetector:
    """OpenCV YuNet face detector (CPU)."""
    name = "yunet"

    def __init__(self, path: Optional[str] = None, score_threshold: float = 0.6) -> None:
        import cv2
        path = path or model_path()
        if not verify_model(path):
            raise RuntimeError(f"face model missing or checksum mismatch: {path}")
        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(path, "", (320, 320), score_threshold, 0.3, 5000)

    @staticmethod
    def available() -> bool:
        try:
            import cv2  # noqa: F401
            return verify_model(model_path())
        except Exception:
            return False

    def detect(self, image: Image.Image) -> list[Face]:
        rgb = np.asarray(image.convert("RGB"))
        bgr = rgb[:, :, ::-1].copy()
        h, w = bgr.shape[:2]
        if w < 16 or h < 16:
            return []
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(bgr)
        out = []
        if faces is None:
            return out
        for row in faces:
            x, y, bw, bh = (int(v) for v in row[:4])
            lm = [(float(row[4 + 2 * i]), float(row[5 + 2 * i])) for i in range(5)]
            out.append(Face((max(0, x), max(0, y), max(1, bw), max(1, bh)), lm, float(row[14])))
        return out


@dataclass
class PresenterObservation:
    valid: bool
    face: Optional[Face] = None
    motion: float = 0.0            # normalised in-face motion (0..1)
    region_changed: bool = True    # presenter region pixels differ from the previous fresh frame
    frame_changed_elsewhere: bool = True   # rest of the frame changed (so a frozen region is meaningful)
    note: str = ""
    faces: int = 0
    ambiguous: str = ""                    # "multiple faces" | "face too small" | "" — presence not judged when set


class PresenterAnalyzer:
    """Stateful analyser over consecutive fresh frames of the presenter region."""

    FROZEN_EPSILON = 0.0015   # mean abs luminance diff (0..1) below which the region counts as pixel-identical

    def __init__(self, backend: FaceDetectorBackend, motion_threshold: float = 0.035, noise_floor: float = 0.012,
                 min_face_px: int = 24) -> None:
        self.backend = backend
        self.min_face_px = min_face_px
        self.motion_threshold = motion_threshold
        self.noise_floor = noise_floor
        self._prev_region: Optional[np.ndarray] = None
        self._prev_face: Optional[np.ndarray] = None
        self._prev_face_box: Optional[tuple[int, int, int, int]] = None
        self._prev_outside_hash: Optional[str] = None
        self._prev_region_hash: Optional[str] = None

    @staticmethod
    def _gray(img: Image.Image, size: Optional[tuple[int, int]] = None) -> np.ndarray:
        g = img.convert("L").filter(ImageFilter.GaussianBlur(1.2))
        if size:
            g = g.resize(size, Image.BILINEAR)
        return np.asarray(g, dtype=np.float32) / 255.0

    def observe(self, frame: Image.Image, region_box: tuple[int, int, int, int], fresh: bool) -> PresenterObservation:
        if not fresh:
            return PresenterObservation(valid=False, note="no fresh frame")
        region = frame.crop(region_box)
        if region.width < 32 or region.height < 32:
            return PresenterObservation(valid=False, note="presenter region too small")
        # frame change outside the region (for frozen-preview evidence)
        outside = frame.copy()
        from PIL import ImageDraw
        ImageDraw.Draw(outside).rectangle(region_box, fill=(0, 0, 0))
        outside_hash = hashlib.md5(outside.convert("L").resize((64, 36)).tobytes()).hexdigest()
        # unblurred, lightly downscaled luminance: a live camera always carries sensor noise, a frozen
        # buffer repeats pixel-identical content, so "unchanged" means essentially zero difference.
        region_small = np.asarray(region.convert("L"), dtype=np.float32) / 255.0   # native resolution, no averaging
        frame_changed = self._prev_outside_hash is None or outside_hash != self._prev_outside_hash
        if self._prev_region is None or self._prev_region.shape != region_small.shape:
            region_changed = True
        else:
            region_changed = float(np.mean(np.abs(region_small - self._prev_region))) > self.FROZEN_EPSILON
        self._prev_outside_hash = outside_hash
        self._prev_region = region_small

        faces = self.backend.detect(region)
        face = max(faces, key=lambda f: f.box[2] * f.box[3]) if faces else None
        ambiguous = ""
        if face is not None and (face.box[2] < self.min_face_px or face.box[3] < self.min_face_px):
            ambiguous = "face too small for reliable evaluation"
        elif len(faces) > 1:
            second = sorted(faces, key=lambda f: f.box[2] * f.box[3])[-2]
            if second.box[2] * second.box[3] >= 0.5 * face.box[2] * face.box[3]:
                ambiguous = f"multiple faces ({len(faces)}); presenter ambiguous"
        motion = 0.0
        if face is not None:
            x, y, w, h = face.box
            crop = region.crop((x, y, min(region.width, x + w), min(region.height, y + h)))
            cur = self._gray(crop, (48, 48))
            if self._prev_face is not None and self._prev_face_box is not None:
                diff = float(np.mean(np.abs(cur - self._prev_face)))
                # landmark/box displacement normalised by face size
                px, py, pw, ph = self._prev_face_box
                disp = (abs(x - px) + abs(y - py)) / max(1.0, float(w + h))
                motion = max(0.0, diff - self.noise_floor) + min(1.0, disp)
            self._prev_face, self._prev_face_box = cur, face.box
        else:
            self._prev_face = self._prev_face_box = None
        return PresenterObservation(valid=True, face=face, motion=motion, region_changed=region_changed,
                                    frame_changed_elsewhere=frame_changed, faces=len(faces), ambiguous=ambiguous)

    def reset(self) -> None:
        self._prev_region = self._prev_face = None
        self._prev_face_box = self._prev_outside_hash = self._prev_region_hash = None


def black_level(frame: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Mean luminance (0..255) of a region; used by the black-preview check."""
    region = frame.crop(box).convert("L").resize((32, 32))
    return float(np.asarray(region, dtype=np.float32).mean())
