"""Watchdog: (1) an in-process stall detector for the monitor loop and (2) a
process supervisor (`studio-monitor supervise`) that restarts the monitor
when it exits unexpectedly, with backoff and an hourly cap. Both are plain
bookkeeping; neither touches Studio."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)


class StallDetector:
    """The monitor loop calls ``beat()`` after every tick; ``check()`` reports a
    stall when no beat arrived for ``stall_after`` seconds (returns the gap)."""

    def __init__(self, stall_after: float = 120.0, mono: Callable[[], float] = time.monotonic) -> None:
        self.stall_after = stall_after
        self.mono = mono
        self._last = mono()
        self.stalls = 0
        self._reported = False

    def beat(self) -> None:
        self._last = self.mono()
        self._reported = False

    def check(self) -> Optional[float]:
        gap = self.mono() - self._last
        if gap >= self.stall_after and not self._reported:
            self._reported = True
            self.stalls += 1
            return gap
        return None


@dataclass
class SupervisorPolicy:
    backoff_base: float = 5.0
    backoff_max: float = 300.0
    max_restarts_per_hour: int = 10


@dataclass
class SupervisorState:
    restarts: int = 0
    exits: list = field(default_factory=list)      # (mono, returncode)
    last_error: str = ""
    gave_up: bool = False


class Supervisor:
    """Runs ``spawn()`` (a Popen-like object with ``poll()``/``wait()``/``returncode``) and restarts it when it
    exits with a non-zero code. ``sleep``/``mono`` are injectable for tests."""

    def __init__(self, spawn: Callable[[], object], policy: SupervisorPolicy = SupervisorPolicy(),
                 mono: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 on_event: Optional[Callable[[str], None]] = None, wait_poll: float = 1.0) -> None:
        self.spawn = spawn
        self.policy = policy
        self.mono, self.sleep = mono, sleep
        self.on_event = on_event or (lambda m: log.info(m))
        self.state = SupervisorState()
        self._stop = threading.Event()
        self.wait_poll = wait_poll

    def stop(self) -> None:
        self._stop.set()

    def _recent_restarts(self) -> int:
        cutoff = self.mono() - 3600
        return sum(1 for t, _rc in self.state.exits if t >= cutoff)

    def run_once(self) -> Optional[int]:
        """Spawn and wait for one child; returns its exit code (None if stopped)."""
        try:
            proc = self.spawn()
        except Exception as exc:
            self.state.last_error = f"spawn failed: {exc}"[:200]
            self.on_event(self.state.last_error)
            return 1
        while not self._stop.is_set():
            rc = proc.poll()
            if rc is not None:
                return rc
            self.sleep(self.wait_poll)
        try:
            proc.terminate()
        except Exception:  # pragma: no cover
            pass
        return None

    def run(self, max_cycles: Optional[int] = None) -> SupervisorState:
        cycles = 0
        while not self._stop.is_set():
            rc = self.run_once()
            cycles += 1
            if rc is None:
                break
            if rc == 0:
                self.on_event("monitor exited normally; supervisor stops")
                break
            self.state.exits.append((self.mono(), rc))
            if self._recent_restarts() > self.policy.max_restarts_per_hour:
                self.state.gave_up = True
                self.on_event(f"monitor crashed {self._recent_restarts()} times within an hour (last exit code {rc}); giving up")
                break
            delay = min(self.policy.backoff_max, self.policy.backoff_base * (2 ** min(10, self.state.restarts)))
            self.state.restarts += 1
            self.on_event(f"monitor exited with code {rc}; restart #{self.state.restarts} in {delay:.0f} s")
            self.sleep(delay)
            if max_cycles is not None and cycles >= max_cycles:
                break
        return self.state


def default_spawn(argv: list[str]) -> Callable[[], subprocess.Popen]:
    """Spawn the monitor as a child process (the frozen exe when running frozen, else the module)."""
    def _spawn() -> subprocess.Popen:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            cmd = [exe, *argv]
        else:
            cmd = [sys.executable, "-m", "studio_monitor", *argv]
        return subprocess.Popen(cmd)
    return _spawn
