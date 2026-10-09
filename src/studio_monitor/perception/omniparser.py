"""Optional external screen parser (Microsoft OmniParser icon detector).

Not bundled: the detector weights and torch are installed by the operator in
a separate Python environment (see docs/perception/OMNIPARSER_EVALUATION.md
for the licence review and the measured benchmark). Inference runs in a
*separate process* with a timeout so it can never block capture or the GUI.
Results are generic "interactive element" boxes; they refine, never replace,
the evidence-based layout."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from .layout import LayoutElement

RUNNER = Path(__file__).with_name("omniparser_runner.py")


class OmniParserBackend:
    name = "omniparser"

    def __init__(self, python: str, model: str, timeout: float = 20.0, device: str = "auto", conf: float = 0.3) -> None:
        self.python, self.model, self.timeout, self.device, self.conf = python, model, timeout, device, conf
        self.last_ms = 0.0
        self.last_error = ""

    @classmethod
    def available(cls, python: str, model: str) -> bool:
        return bool(python and model and os.path.exists(python) and os.path.exists(model))

    def __call__(self, frame: Image.Image) -> list[LayoutElement]:
        if not self.available(self.python, self.model):
            self.last_error = "parser environment or model not configured"
            return []
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "frame.png"
            frame.save(src, format="PNG")
            t0 = time.monotonic()
            try:
                proc = subprocess.run([self.python, str(RUNNER), "--model", self.model, "--image", str(src), "--device", self.device,
                                       "--conf", str(self.conf)], capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self.last_error = f"parser timed out after {self.timeout:.0f} s"
                return []
            self.last_ms = (time.monotonic() - t0) * 1000
            if proc.returncode != 0:
                self.last_error = (proc.stderr or proc.stdout)[-300:]
                return []
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                self.last_error = "parser returned no JSON"
                return []
        out = []
        for i, d in enumerate(data.get("boxes", [])):
            x, y, x2, y2 = d["box"]
            out.append(LayoutElement("ui_element", (int(x), int(y), int(x2), int(y2)), float(d.get("conf", 0.0)), "omniparser",
                                     frame_ts=time.time(), detail=f"interactable #{i}"))
        self.last_error = ""
        return out
