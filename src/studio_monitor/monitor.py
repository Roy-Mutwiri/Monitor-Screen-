"""Monitoring loop: track the Studio window, capture it and its dialogs, OCR,
match rules, de-duplicate, and queue Telegram alerts."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .alerts import format_alert, format_status_alert
from .config import AppConfig, TargetIdentity
from .detection.detector import Detection, Detector
from .detection.rules import RuleSet
from .incidents import Incident, IncidentTracker
from .ocr.base import OcrBackend
from .privacy import purge_old_screenshots, redact
from .queue import DeliveryQueue, DeliveryWorker
from .target import related_windows
from .tracker import Status, WindowTracker
from .win32.capture import Capture, Capturer
from .win32.windows import WindowSystem

log = logging.getLogger(__name__)


@dataclass
class StatusUpdate:
    status: Status
    reason: str
    window_title: str = ""
    queue_counts: Optional[dict] = None


class Monitor:
    def __init__(self, cfg: AppConfig, system: WindowSystem, capturer: Capturer, ocr: OcrBackend,
                 rules: RuleSet, queue: DeliveryQueue, sender: Optional[Callable[[dict, str], None]] = None,
                 clock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[str], None]] = None,
                 on_status: Optional[Callable[[StatusUpdate], None]] = None,
                 on_capture: Optional[Callable[[Capture], None]] = None,
                 on_identity_change: Optional[Callable[[TargetIdentity], None]] = None) -> None:
        self.cfg = cfg
        self.system = system
        self.capturer = capturer
        self.queue = queue
        self.clock = clock
        self.on_event = on_event or (lambda msg: log.info(msg))
        self.on_status = on_status or (lambda s: None)
        self.on_capture = on_capture or (lambda c: None)
        self._on_identity_change = on_identity_change
        self.tracker = WindowTracker(system, cfg.target, clock, on_identity_change=self._identity_changed)
        self.detector = Detector(ocr, rules, log_text=cfg.privacy.log_ocr_text)
        self.incidents = IncidentTracker(
            confirm_polls=cfg.detection.confirm_polls,
            cooldown_seconds=cfg.detection.dedup_cooldown_seconds,
            resolve_after_seconds=cfg.detection.resolve_after_seconds,
            clock=clock,
        )
        self.worker: Optional[DeliveryWorker] = None
        if sender is not None:
            self.worker = DeliveryWorker(queue, sender, on_event=self.on_event)
        self._stop = threading.Event()
        self._last_status: tuple[Status, str] = (Status.STOPPED, "")
        self._last_purge = 0.0
        self.last_detections: list[Detection] = []

    # ------------------------------------------------------------------
    def _identity_changed(self, identity: TargetIdentity) -> None:
        self.cfg.target = identity
        if self._on_identity_change:
            self._on_identity_change(identity)

    def _emit_status(self, status: Status, reason: str) -> None:
        title = self.tracker.state.window.title if self.tracker.state.window else ""
        self.on_status(StatusUpdate(status, reason, title, self.queue.counts()))
        if (status, reason) != self._last_status:
            self._last_status = (status, reason)
            if self.cfg.telegram.notify_status_changes and status in (Status.LOST, Status.DEGRADED, Status.RUNNING):
                text = format_status_alert(status.value, reason, self.cfg.machine_label, self.clock())
                self.queue.enqueue("STATUS", {"text": text, "caption": text}, "")
                if self.worker:
                    self.worker.kick()

    # ------------------------------------------------------------------
    def tick(self) -> list[Detection]:
        """One monitoring poll. Safe to call directly (tests, CLI)."""
        state = self.tracker.poll()
        for ev in self.tracker.drain_events():
            self.on_event(ev)
        self.last_detections = []
        if state.status != Status.RUNNING or state.window is None:
            self.incidents.tick()
            self._emit_status(state.status, state.reason)
            return []

        main = state.window
        foreground = self.system.foreground_window()
        captures: list[Capture] = []
        cap = self.capturer.capture(main, foreground)
        self.tracker.report_capture(cap is not None, bool(cap and cap.reliable), cap.note if cap else "")
        if cap is not None:
            captures.append(cap)
        if self.cfg.detection.include_dialogs:
            for win in related_windows(self.system, main):
                dcap = self.capturer.capture(win, foreground)
                if dcap is not None:
                    dcap.is_dialog = True
                    captures.append(dcap)

        for ev in self.tracker.drain_events():
            self.on_event(ev)
        if captures:
            self.on_capture(captures[0])

        detections: list[Detection] = []
        for c in captures:
            # Redaction happens before OCR so redacted areas are never read or sent.
            c.image = redact(c.image, self.cfg.regions)
            regions = [] if c.is_dialog else [r for r in self.cfg.regions if r.kind == "detect"]
            det = self.detector.detect(c, regions)
            if det is not None:
                detections.append(det)

        for det in detections:
            decision = self.incidents.observe(
                det.category, det.match.label, det.ocr_text,
                manual_attention=det.match.manual_attention,
                window_title=det.capture.window.title, is_dialog=det.is_dialog,
            )
            if decision.alert and decision.incident is not None:
                self._raise_alert(det, decision.incident, decision.reason)
            else:
                log.debug("detection %s suppressed: %s", det.category, decision.reason)
        self.incidents.tick()
        self.last_detections = detections
        self._emit_status(self.tracker.state.status, self.tracker.state.reason)
        self._maybe_purge()
        return detections

    def _raise_alert(self, det: Detection, inc: Incident, reason: str) -> None:
        shots = self.cfg.screenshots_dir
        shots.mkdir(parents=True, exist_ok=True)
        path = shots / f"{inc.incident_id}.png"
        try:
            det.capture.image.save(path, format="PNG")
            inc.screenshot_path = str(path)
        except OSError as exc:  # pragma: no cover
            log.error("could not save screenshot: %s", exc)
            inc.screenshot_path = ""
        attach = self.cfg.privacy.send_screenshots and bool(inc.screenshot_path)
        payload = format_alert(
            inc, self.cfg.machine_label, self.cfg.privacy.max_text_in_alert,
            screenshot_attached=attach, reason=reason if reason != "new incident" else "",
            capture_method=det.capture.method,
        )
        stored_text = inc.text if self.cfg.privacy.store_detected_text else ""
        self.queue.record_incident(inc.incident_id, inc.category, inc.label, stored_text,
                                   inc.window_title, inc.is_dialog, inc.screenshot_path)
        self.queue.enqueue(inc.incident_id, payload, inc.screenshot_path if attach else "")
        self.on_event(
            f"{'MANUAL ATTENTION: ' if inc.manual_attention else ''}{inc.label} detected in "
            f"{'dialog' if inc.is_dialog else 'main window'} -> {inc.incident_id} queued ({reason})"
        )
        if self.worker:
            self.worker.kick()

    def _maybe_purge(self) -> None:
        now = self.clock()
        if now - self._last_purge < 3600:
            return
        self._last_purge = now
        removed = purge_old_screenshots(self.cfg.screenshots_dir, self.cfg.privacy, now)
        self.queue.purge_sent(30 * 86400)
        if removed:
            self.on_event(f"privacy: removed {removed} screenshot(s) past retention")

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Blocking loop until :meth:`stop` is called."""
        if self.worker and not self.worker.is_alive():
            self.worker.start()
        self.on_event("monitoring started")
        try:
            while not self._stop.is_set():
                started = self.clock()
                try:
                    self.tick()
                except Exception as exc:  # keep the loop alive
                    log.exception("monitor tick failed")
                    self.on_event(f"error during poll: {exc}")
                elapsed = self.clock() - started
                self._stop.wait(max(0.2, self.cfg.detection.poll_interval_seconds - elapsed))
        finally:
            self.tracker.stop()
            self._emit_status(Status.STOPPED, "")
            self.on_event("monitoring stopped")

    def stop(self) -> None:
        self._stop.set()
        if self.worker:
            self.worker.stop()

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="studio-monitor", daemon=True)
        thread.start()
        return thread


def ensure_dirs(cfg: AppConfig) -> Path:
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    cfg.screenshots_dir.mkdir(parents=True, exist_ok=True)
    return cfg.data_path
