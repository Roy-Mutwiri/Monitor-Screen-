"""PC health: CPU, memory, disk, battery, Studio process load and upload
throughput (psutil, injectable). Sustained thresholds become one
``PC_HEALTH`` incident per condition episode; recovery resolves it. Values
are measurements, never inferences, and nothing here changes thresholds."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .detectors.text_rules import SustainedCondition


@dataclass
class PcHealthConfig:
    enabled: bool = True
    interval_seconds: float = 30.0
    cpu_percent: float = 90.0
    memory_percent: float = 90.0
    disk_free_percent: float = 5.0
    battery_percent: float = 20.0           # only when discharging
    upload_kbps_min: float = 0.0            # 0 = disabled; checked only while LIVE
    sustain_seconds: float = 120.0
    recover_seconds: float = 60.0


@dataclass
class PcSample:
    ts: float
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_free_percent: float = 100.0
    battery_percent: Optional[float] = None
    on_battery: bool = False
    studio_cpu_percent: Optional[float] = None
    studio_memory_mb: Optional[float] = None
    upload_kbps: Optional[float] = None
    download_kbps: Optional[float] = None
    error: str = ""

    def to_dict(self) -> dict:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


CONDITIONS = {
    "CPU_HIGH": "CPU usage above {thr:.0f}% for {d:.0f} s ({val:.0f}%).",
    "MEMORY_HIGH": "Memory usage above {thr:.0f}% for {d:.0f} s ({val:.0f}%).",
    "DISK_LOW": "Free disk space on the monitor's data drive below {thr:.0f}% for {d:.0f} s ({val:.1f}% free).",
    "BATTERY_LOW": "Running on battery below {thr:.0f}% for {d:.0f} s ({val:.0f}%).",
    "UPLOAD_LOW": "Upload throughput below {thr:.0f} kbps while LIVE for {d:.0f} s ({val:.0f} kbps).",
}
RECOVERED = {"CPU_HIGH": "CPU usage back below threshold.", "MEMORY_HIGH": "Memory usage back below threshold.",
             "DISK_LOW": "Free disk space back above threshold.", "BATTERY_LOW": "Power restored or battery above threshold.",
             "UPLOAD_LOW": "Upload throughput back above threshold."}


class PcHealthSampler:
    def __init__(self, cfg: PcHealthConfig, data_dir: str, ps: Any = None, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.data_dir = data_dir
        if ps is None:
            try:
                import psutil as ps  # type: ignore
            except Exception:  # pragma: no cover
                ps = None
        self.ps = ps
        self.clock, self.mono = clock, mono
        self._next = 0.0
        self._last_net: Optional[tuple[float, int, int]] = None
        self._proc: Any = None
        self._proc_pid = 0
        self.last: Optional[PcSample] = None
        self.cond = {k: SustainedCondition(k, cfg.sustain_seconds, cfg.recover_seconds, max_gap_seconds=cfg.interval_seconds * 3)
                     for k in CONDITIONS}

    # ------------------------------------------------------------------
    def due(self) -> bool:
        return self.cfg.enabled and self.ps is not None and self.mono() >= self._next

    def sample(self, studio_pid: int = 0) -> PcSample:
        s = PcSample(self.clock())
        ps = self.ps
        try:
            s.cpu_percent = float(ps.cpu_percent(interval=None))
            s.memory_percent = float(ps.virtual_memory().percent)
            try:
                du = ps.disk_usage(os.path.splitdrive(self.data_dir)[0] + os.sep if os.path.splitdrive(self.data_dir)[0] else self.data_dir)
                s.disk_free_percent = 100.0 - float(du.percent)
            except Exception:
                pass
            try:
                b = ps.sensors_battery() if hasattr(ps, "sensors_battery") else None
                if b is not None:
                    s.battery_percent, s.on_battery = float(b.percent), not bool(b.power_plugged)
            except Exception:
                pass
            try:
                io = ps.net_io_counters()
                now = self.mono()
                if self._last_net is not None and now > self._last_net[0]:
                    dt = now - self._last_net[0]
                    s.upload_kbps = (io.bytes_sent - self._last_net[1]) * 8 / 1000.0 / dt
                    s.download_kbps = (io.bytes_recv - self._last_net[2]) * 8 / 1000.0 / dt
                self._last_net = (now, io.bytes_sent, io.bytes_recv)
            except Exception:
                pass
            if studio_pid:
                try:
                    if self._proc is None or self._proc_pid != studio_pid:
                        self._proc, self._proc_pid = ps.Process(studio_pid), studio_pid
                        self._proc.cpu_percent(interval=None)
                    s.studio_cpu_percent = float(self._proc.cpu_percent(interval=None))
                    s.studio_memory_mb = float(self._proc.memory_info().rss) / (1024 * 1024)
                except Exception:
                    self._proc = None
        except Exception as exc:  # pragma: no cover - defensive
            s.error = f"{type(exc).__name__}: {exc}"[:200]
        self.last = s
        self._next = self.mono() + self.cfg.interval_seconds
        return s

    def evaluate(self, s: PcSample, live: bool) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Returns ([(condition, text)], [(condition, text)]) for newly confirmed / recovered conditions."""
        c = self.cfg
        now = self.mono()
        checks = {
            "CPU_HIGH": (s.cpu_percent >= c.cpu_percent, True, s.cpu_percent, c.cpu_percent),
            "MEMORY_HIGH": (s.memory_percent >= c.memory_percent, True, s.memory_percent, c.memory_percent),
            "DISK_LOW": (s.disk_free_percent <= c.disk_free_percent, True, s.disk_free_percent, c.disk_free_percent),
            "BATTERY_LOW": (bool(s.on_battery and s.battery_percent is not None and s.battery_percent <= c.battery_percent),
                            s.battery_percent is not None, s.battery_percent or 0.0, c.battery_percent),
            "UPLOAD_LOW": (bool(c.upload_kbps_min and live and s.upload_kbps is not None and s.upload_kbps < c.upload_kbps_min),
                           bool(c.upload_kbps_min) and live and s.upload_kbps is not None, s.upload_kbps or 0.0, c.upload_kbps_min),
        }
        confirmed, recovered = [], []
        for name, (active, valid, val, thr) in checks.items():
            cond = self.cond[name]
            res = cond.update(active, valid and not s.error, now, f"{val:.1f}")
            if res == "confirmed":
                confirmed.append((name, CONDITIONS[name].format(thr=thr, d=cond.duration(now) or c.sustain_seconds, val=val)))
            elif res == "recovered":
                recovered.append((name, RECOVERED[name]))
        return confirmed, recovered

    def snapshot(self) -> dict:
        out = {"sample": self.last.to_dict() if self.last else {}, "problems": [k for k, c in self.cond.items() if c.confirmed]}
        return out
