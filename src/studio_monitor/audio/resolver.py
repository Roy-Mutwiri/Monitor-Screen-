"""AudioSourceResolver: decide which audio evidence is available for Studio
without touching routing. Inputs: the Studio process tree, Windows audio
sessions (pycaw), audio endpoints, device names Studio shows in its own UI
(OCR'd mixer/source labels), OS support for process loopback, and the
operator's preference. Output: one binding with a clear label, or a short
list of choices when the association is ambiguous."""
from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

KIND_LOOPBACK, KIND_SESSION, KIND_INPUT, KIND_VISUAL, KIND_NONE = "studio_loopback", "studio_session", "input_device", "visual_meter", "unavailable"
LABELS = {
    KIND_LOOPBACK: "Studio rendered audio (process loopback; may exclude mic audio Studio does not play locally)",
    KIND_SESSION: "Studio audio session level meter (level only, no speech detection)",
    KIND_INPUT: "Verified selected input device",
    KIND_VISUAL: "Studio visual meter only",
    KIND_NONE: "Audio unavailable / association uncertain",
}


@dataclass
class AudioEndpoint:
    name: str
    is_input: bool
    active: bool
    id: str = ""


@dataclass
class AudioSession:
    pid: int
    process_name: str
    active: bool
    endpoint_name: str = ""


@dataclass
class AudioBinding:
    kind: str
    label: str
    confidence: float
    detail: str = ""
    pid: int = 0
    endpoint: Optional[AudioEndpoint] = None
    choices: list[str] = field(default_factory=list)       # when ambiguous: offered source names
    samples: bool = False                                   # True when real samples are available (VAD possible)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "confidence": self.confidence, "detail": self.detail, "pid": self.pid,
                "endpoint": self.endpoint.name if self.endpoint else "", "choices": self.choices, "samples": self.samples}


def process_loopback_supported(version: str = "") -> bool:
    """Process loopback needs Windows 10 build 20348 or later (the MS sample's stated minimum)."""
    v = version or platform.version()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v)
    if not m or platform.system() != "Windows" and not version:
        return False
    return int(m.group(3)) >= 20348


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class AudioSourceResolver:
    def __init__(self, *, sessions: Callable[[], list[AudioSession]], endpoints: Callable[[], list[AudioEndpoint]],
                 loopback_supported: Optional[bool] = None, loopback_probe: Optional[Callable[[int], bool]] = None) -> None:
        self.sessions_fn, self.endpoints_fn = sessions, endpoints
        self.loopback_supported = process_loopback_supported() if loopback_supported is None else loopback_supported
        self.loopback_probe = loopback_probe
        self.last_sessions: list[AudioSession] = []
        self.last_endpoints: list[AudioEndpoint] = []

    def resolve(self, studio_pids: list[int], ui_labels: list[str], preference: str = "auto") -> AudioBinding:
        """``ui_labels`` are device/source names read from Studio's own UI (OCR); ``preference`` is
        auto | studio_loopback | studio_session | input:<endpoint name> | visual | off."""
        try:
            self.last_sessions = self.sessions_fn()
        except Exception as exc:
            self.last_sessions = []
            sess_err = str(exc)[:100]
        else:
            sess_err = ""
        try:
            self.last_endpoints = self.endpoints_fn()
        except Exception:
            self.last_endpoints = []
        if preference == "off":
            return AudioBinding(KIND_NONE, "Audio listening disabled", 1.0, "disabled by setting")
        if preference == "visual":
            return AudioBinding(KIND_VISUAL, LABELS[KIND_VISUAL], 1.0, "chosen by setting")
        if preference.startswith("input:"):
            name = preference[6:]
            ep = next((e for e in self.last_endpoints if e.is_input and _norm(e.name) == _norm(name)), None)
            if ep is None:
                return AudioBinding(KIND_NONE, LABELS[KIND_NONE], 0.0, f"chosen input '{name}' not present", choices=self._input_names())
            return AudioBinding(KIND_INPUT, f"{LABELS[KIND_INPUT]}: {ep.name}", 0.9 if ep.active else 0.5, "chosen by setting", endpoint=ep,
                                samples=True)
        studio = [s for s in self.last_sessions if s.pid in studio_pids] if studio_pids else []
        pid = next((s.pid for s in studio if s.active), studio[0].pid if studio else (studio_pids[0] if studio_pids else 0))
        if preference in ("auto", "studio_loopback") and pid and self.loopback_supported:
            # candidates: processes of the Studio tree that own an audio session first, then the rest (bounded)
            candidates = [s.pid for s in studio if s.active] + [s.pid for s in studio if not s.active]
            candidates += [p for p in studio_pids if p not in candidates]
            chosen = 0
            if self.loopback_probe is None:
                chosen = pid
            else:
                for cand in candidates[:6]:
                    try:
                        if self.loopback_probe(cand):
                            chosen = cand
                            break
                    except Exception:
                        continue
            if chosen:
                name = next((s.process_name for s in studio if s.pid == chosen), "")
                return AudioBinding(KIND_LOOPBACK, LABELS[KIND_LOOPBACK], 0.9 if studio else 0.7,
                                    f"process {chosen}{' (' + name + ')' if name else ''}", pid=chosen, samples=True)
        if preference in ("auto", "studio_loopback", "studio_session") and studio:
            return AudioBinding(KIND_SESSION, LABELS[KIND_SESSION], 0.8, f"session of pid {pid}", pid=pid)
        # explicit device named in Studio's UI that matches an input endpoint?
        matches = []
        for lab in ui_labels:
            n = _norm(lab)
            for ep in self.last_endpoints:
                if ep.is_input and ep.active and n and (_norm(ep.name) in n or n in _norm(ep.name)):
                    matches.append(ep)
        matches = list({e.name: e for e in matches}.values())
        if len(matches) == 1 and preference == "auto":
            return AudioBinding(KIND_INPUT, f"{LABELS[KIND_INPUT]}: {matches[0].name}", 0.75, "device named in Studio's UI matches one input endpoint",
                                endpoint=matches[0], samples=True)
        if len(matches) > 1:
            return AudioBinding(KIND_NONE, LABELS[KIND_NONE], 0.3, "several input devices match Studio's labels; choose one",
                                choices=[m.name for m in matches])
        detail = sess_err or ("no Studio audio session" if studio_pids else "Studio not running")
        if not self.loopback_supported:
            detail += "; process loopback unsupported on this Windows build"
        return AudioBinding(KIND_VISUAL, LABELS[KIND_VISUAL], 0.5, detail, choices=self._input_names())

    def _input_names(self) -> list[str]:
        return [e.name for e in self.last_endpoints if e.is_input and e.active][:6]


# ---------------------------------------------------------------- real providers (pycaw)

def pycaw_sessions() -> list[AudioSession]:
    from pycaw.pycaw import AudioUtilities
    out = []
    for s in AudioUtilities.GetAllSessions():
        try:
            name = s.Process.name() if s.Process else ""
            out.append(AudioSession(int(s.ProcessId or 0), name, int(getattr(s, "State", 0) or 0) == 1))
        except Exception:
            continue
    return out


def pycaw_endpoints() -> list[AudioEndpoint]:
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import EDataFlow
    out = []
    try:
        devs = AudioUtilities.GetAllDevices()
    except Exception:
        return out
    for d in devs:
        try:
            name = d.FriendlyName or ""
            state = str(getattr(d, "state", ""))
            is_input = False
            try:
                is_input = getattr(d, "dataflow", None) in (EDataFlow.eCapture, 1)   # 1 == eCapture
            except Exception:
                pass
            out.append(AudioEndpoint(name, is_input, "Active" in state, getattr(d, "id", "") or ""))
        except Exception:
            continue
    return out


def studio_process_tree(root_pids: list[int]) -> list[int]:
    try:
        import psutil
    except Exception:
        return list(root_pids)
    pids = set(root_pids)
    for p in psutil.process_iter(["pid", "name", "ppid"]):
        try:
            if p.info["ppid"] in pids or (p.info["name"] or "").lower().startswith("tiktok live studio"):
                pids.add(p.info["pid"])
        except Exception:
            continue
    return sorted(pids)
