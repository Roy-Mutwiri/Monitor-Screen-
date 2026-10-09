"""Monitoring loop: track the Studio window, capture it and its dialogs, OCR,
match rules, de-duplicate, and queue Telegram alerts.

Also drives the Studio activity features: application session (opened /
closed), latest-frame cache, broadcast-state engine and not-live reminders.
Every notification becomes one event with immutable redacted evidence and one
delivery per enabled, subscribed bot (see :mod:`queue`).
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .alerts import (format_alert, format_not_live_reminder, format_status_alert, format_studio_already_running,
                     format_studio_closed, format_studio_opened)
from .bots import (CAT_HEALTH, CAT_REMINDERS, CAT_RESTRICTIONS, CAT_STUDIO_CLOSED, CAT_STUDIO_OPENED,
                   CAT_VERIFICATION, BotRegistry)
from .broadcast import BroadcastStateEngine, Classification, LiveRules, LiveState
from .config import AppConfig, TargetIdentity
from .detection.detector import Detection, Detector
from .detection.rules import RuleSet
from .framecache import FrameCache
from .incidents import Incident, IncidentTracker
from .ocr.base import OcrBackend
from .privacy import purge_old_screenshots, redact
from .queue import KIND_ACTIVITY, KIND_INCIDENT, KIND_STATUS, Delivery, DeliveryError, DeliveryQueue, DeliveryWorker
from .reminders import EVT_REMINDER_CANCELLED, OfflineReminderEngine, ReminderDue
from .sessions import EVT_ALREADY_RUNNING, EVT_CLOSED, EVT_OPENED, SessionEvent, StudioSessionTracker
from .target import related_windows
from .telegram import ClientFactory, deliver
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


@dataclass
class ActivitySnapshot:
    """What the GUI shows in the "Studio activity" panel."""
    app_state: str = "NOT_RUNNING"
    session_id: str = ""
    live_state: str = LiveState.UNKNOWN.value
    live_evidence: str = ""
    live_rules_verified: bool = False
    last_confirmed_utc: str = ""
    last_observation: str = ""
    offline_seconds: float = 0.0
    remaining_seconds: Optional[float] = None
    accumulating: bool = False
    episode_id: str = ""
    reminders_sent: int = 0
    last_event: str = ""
    delivery: dict = field(default_factory=dict)


def _event_id(prefix: str, ts: float) -> str:
    return f"{prefix}-{datetime.fromtimestamp(ts):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def incident_category(popup_category: str) -> str:
    return CAT_VERIFICATION if popup_category == "verification_puzzle" else CAT_RESTRICTIONS


class Monitor:
    def __init__(self, cfg: AppConfig, system: WindowSystem, capturer: Capturer, ocr: OcrBackend,
                 rules: RuleSet, queue: DeliveryQueue, registry: BotRegistry,
                 client_factory: Optional[ClientFactory] = None,
                 clock: Callable[[], float] = time.time,
                 on_event: Optional[Callable[[str], None]] = None,
                 on_status: Optional[Callable[[StatusUpdate], None]] = None,
                 on_capture: Optional[Callable[[Capture], None]] = None,
                 on_identity_change: Optional[Callable[[TargetIdentity], None]] = None,
                 live_rules: Optional[LiveRules] = None,
                 frame_cache: Optional[FrameCache] = None,
                 mono: Callable[[], float] = time.monotonic,
                 on_activity: Optional[Callable[[ActivitySnapshot], None]] = None) -> None:
        self.cfg = cfg
        self.system = system
        self.capturer = capturer
        self.queue = queue
        self.registry = registry
        self.client_factory = client_factory
        self.clock = clock
        self.mono = mono
        self.on_event = on_event or (lambda msg: log.info(msg))
        self.on_status = on_status or (lambda s: None)
        self.on_capture = on_capture or (lambda c: None)
        self.on_activity = on_activity or (lambda a: None)
        self._on_identity_change = on_identity_change
        self.tracker = WindowTracker(system, cfg.target, clock, on_identity_change=self._identity_changed)
        self.detector = Detector(ocr, rules, log_text=cfg.privacy.log_ocr_text)
        self.incidents = IncidentTracker(
            confirm_polls=cfg.detection.confirm_polls,
            cooldown_seconds=cfg.detection.dedup_cooldown_seconds,
            resolve_after_seconds=cfg.detection.resolve_after_seconds,
            clock=clock,
        )
        act = cfg.activity
        self.frame_cache = frame_cache or FrameCache(cfg.frame_cache_dir, clock, mono)
        self.sessions = StudioSessionTracker(
            system, close_debounce_seconds=act.close_debounce_seconds,
            open_screenshot_timeout_seconds=act.open_screenshot_timeout_seconds,
            notify_already_running=act.notify_already_running, clock=clock, mono=mono,
        )
        self.live_rules = live_rules or LiveRules({})
        self.broadcast = BroadcastStateEngine(self.live_rules, act.confirm_observations,
                                              act.max_observation_gap_seconds, clock, mono)
        self.reminders = OfflineReminderEngine(
            queue, threshold_seconds=act.offline_threshold_minutes * 60.0,
            max_gap_seconds=act.max_observation_gap_seconds, repeat_enabled=act.repeat_enabled,
            repeat_interval_seconds=act.repeat_interval_minutes * 60.0, repeat_max=act.repeat_max_count,
            enabled=act.reminders_enabled, clock=clock, mono=mono,
        )
        self.worker: Optional[DeliveryWorker] = None
        if client_factory is not None:
            self.worker = DeliveryWorker(queue, self.send_delivery, cfg.telegram.delivery_concurrency,
                                         on_event=self.on_event)
            registry.listeners.append(self._registry_changed)
        self._stop = threading.Event()
        self._last_status: tuple[Status, str] = (Status.STOPPED, "")
        self._last_purge = 0.0
        self.last_detections: list[Detection] = []
        self.activity = ActivitySnapshot(live_rules_verified=self.live_rules.verified)

    # ------------------------------------------------------------------
    def _identity_changed(self, identity: TargetIdentity) -> None:
        self.cfg.target = identity
        if self._on_identity_change:
            self._on_identity_change(identity)

    def _registry_changed(self, action: str, bot_id: str) -> None:
        if self.client_factory is not None and action in ("token", "removed"):
            self.client_factory.invalidate(bot_id)
        self._kick()

    def send_delivery(self, d: Delivery) -> Optional[int]:
        """Worker callback: send one delivery with that bot's own token/destination."""
        assert self.client_factory is not None
        client = self.client_factory.client(d.bot_id, d.chat_id, d.thread_id)
        if client is None:
            raise DeliveryError("bot token not available in the credential store", permanent=True)
        result = deliver(client, d.payload, d.evidence_path, self.clock)
        return result.get("message_id") if isinstance(result, dict) else None

    def dispatch(self, event_id: str, kind: str, category: str, payload: dict, evidence_path: str,
                 label: str = "") -> int:
        """Create one event + one delivery per enabled subscribed bot."""
        targets = self.registry.targets(category)
        n = self.queue.create_event(event_id, kind, category, payload, evidence_path, targets, label)
        self._kick()
        return n

    def _emit_status(self, status: Status, reason: str) -> None:
        title = self.tracker.state.window.title if self.tracker.state.window else ""
        self.on_status(StatusUpdate(status, reason, title, self.queue.counts()))
        if (status, reason) != self._last_status:
            self._last_status = (status, reason)
            if status in (Status.LOST, Status.DEGRADED, Status.RUNNING) and self.registry.targets(CAT_HEALTH):
                text = format_status_alert(status.value, reason, self.cfg.machine_label, self.clock())
                self.dispatch(_event_id("HLT", self.clock()), KIND_STATUS, CAT_HEALTH,
                              {"text": text, "caption": text, "created_at": self.clock()}, "",
                              label=f"Monitor {status.value}")

    def _kick(self) -> None:
        if self.worker:
            self.worker.kick()

    # ------------------------------------------------------------------
    def tick(self) -> list[Detection]:
        """One monitoring poll. Safe to call directly (tests, CLI)."""
        state = self.tracker.poll()
        for ev in self.tracker.drain_events():
            self.on_event(ev)
        self.last_detections = []
        window_present = state.window is not None and state.status in (Status.RUNNING, Status.DEGRADED)

        captures: list[Capture] = []
        detections: list[Detection] = []
        main_cap: Optional[Capture] = None
        main_full_text: Optional[str] = None
        if state.status == Status.RUNNING and state.window is not None:
            main = state.window
            foreground = self.system.foreground_window()
            cap = self.capturer.capture(main, foreground)
            self.tracker.report_capture(cap is not None, bool(cap and cap.reliable), cap.note if cap else "")
            if cap is not None:
                captures.append(cap)
                main_cap = cap
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

            for c in captures:
                # Redaction happens before OCR so redacted areas are never read, stored or sent.
                c.image = redact(c.image, self.cfg.regions)
                regions = [] if c.is_dialog else [r for r in self.cfg.regions if r.kind == "detect"]
                det = self.detector.detect(c, regions)
                if c is main_cap:
                    main_full_text = self.detector.last_full_text
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

        # ---- Studio activity: session, frame cache, broadcast state, reminders
        screenshot_ok = main_cap is not None and main_cap.reliable
        if screenshot_ok:
            self.frame_cache.update(main_cap.image)
        self._update_sessions(window_present, screenshot_ok)
        popup_on_main = any(not d.is_dialog for d in detections)
        classification: Optional[Classification] = None
        if screenshot_ok and not popup_on_main and self.sessions.running:
            classification = self._classify_live(main_cap, main_full_text)
        self._update_broadcast_and_reminders(classification)

        self._emit_status(self.tracker.state.status, self.tracker.state.reason)
        self._emit_activity()
        self._maybe_purge()
        return detections

    # ---- restriction alerts ------------------------------------------
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
        payload["created_at"] = self.clock()
        stored_text = inc.text if self.cfg.privacy.store_detected_text else ""
        self.queue.record_incident(inc.incident_id, inc.category, inc.label, stored_text,
                                   inc.window_title, inc.is_dialog, inc.screenshot_path)
        event_id = inc.incident_id if inc.alerts_sent <= 1 else f"{inc.incident_id}-R{inc.alerts_sent}"
        n = self.dispatch(event_id, KIND_INCIDENT, incident_category(inc.category), payload,
                          inc.screenshot_path if attach else "", label=inc.label)
        self.on_event(
            f"{'MANUAL ATTENTION: ' if inc.manual_attention else ''}{inc.label} detected in "
            f"{'dialog' if inc.is_dialog else 'main window'} -> {event_id} queued for {n} bot(s) ({reason})"
        )

    # ---- sessions ----------------------------------------------------
    def _update_sessions(self, window_present: bool, screenshot_ok: bool) -> None:
        self.sessions.update(self.tracker.identity, window_present, screenshot_ok)
        for ev in self.sessions.drain_events():
            try:
                self._handle_session_event(ev)
            except Exception:  # pragma: no cover
                log.exception("session event handling failed")

    def _activity_shot(self, event_id: str, frame) -> str:
        if frame is None or not self.cfg.privacy.send_screenshots:
            return ""
        dest = self.cfg.activity_screenshots_dir / f"{event_id}.png"
        return self.frame_cache.export(dest) or ""

    def _handle_session_event(self, ev: SessionEvent) -> None:
        act = self.cfg.activity
        eid = _event_id("EVT", ev.ts)
        if ev.type == EVT_OPENED:
            summary, category = "Studio opened", CAT_STUDIO_OPENED
            if not act.notify_opened:
                self.queue.record_event(eid, ev.type, ev.ts_utc, {"summary": summary, "notified": False}, ev.session_id)
                self.on_event(f"{summary} (session {ev.session_id}); notification disabled")
                return
            frame = self.frame_cache.fresh(act.fresh_screenshot_max_age_seconds) if ev.screenshot_available else None
            shot = self._activity_shot(eid, frame)
            payload = format_studio_opened(self.cfg.machine_label, ev.ts, bool(shot), act.open_screenshot_timeout_seconds)
        elif ev.type == EVT_ALREADY_RUNNING:
            summary, category = "Studio already running at monitor start", CAT_STUDIO_OPENED
            frame = self.frame_cache.fresh(act.fresh_screenshot_max_age_seconds) if ev.screenshot_available else None
            shot = self._activity_shot(eid, frame)
            payload = format_studio_already_running(self.cfg.machine_label, ev.ts, bool(shot))
        elif ev.type == EVT_CLOSED:
            summary, category = f"Studio closed ({ev.note})", CAT_STUDIO_CLOSED
            if not act.notify_closed:
                self.queue.record_event(eid, ev.type, ev.ts_utc, {"summary": summary, "notified": False}, ev.session_id)
                self.on_event(f"{summary}; notification disabled")
                return
            frame = self.frame_cache.latest()   # last frame *before* closure, whatever its age
            shot = self._activity_shot(eid, frame)
            payload = format_studio_closed(self.cfg.machine_label, ev.ts, frame.captured_at if shot else None)
        else:  # pragma: no cover
            return
        n = self.dispatch(eid, KIND_ACTIVITY, category, payload, shot, label=summary)
        self.queue.record_event(eid, ev.type, ev.ts_utc,
                                {"summary": summary, "notified": True, "screenshot": bool(shot), "note": ev.note,
                                 "bots": n}, ev.session_id, "", shot, None)
        self.on_event(f"{summary} -> {eid} queued for {n} bot(s){'' if shot else ' (text only)'}")

    # ---- broadcast state + reminders ---------------------------------
    def _classify_live(self, cap: Capture, full_text: Optional[str]) -> Classification:
        regions = self.cfg.live_regions
        if regions:
            text = "\n".join(self.detector.ocr_text(r.crop(cap.image)) for r in regions)
        elif full_text is not None:
            text = full_text
        else:
            text = self.detector.ocr_text(cap.image)
        return self.live_rules.classify(text)

    def _update_broadcast_and_reminders(self, classification: Optional[Classification]) -> None:
        cs = self.broadcast.observe(classification)
        for old, new, why in self.broadcast.drain_transitions():
            self.on_event(f"broadcast state {old.value} -> {new.value} ({why})")
        session = self.sessions.state.session
        out = self.reminders.update(session is not None, session.pid if session else 0, cs.state, cs.fresh)
        if out.episode_started:
            self.on_event(f"offline episode {self.reminders.state.episode_id} started (confirmed NOT_LIVE)")
        for event_id, reason in out.cancelled:
            eid = _event_id("EVT", self.clock())
            self.queue.record_event(eid, EVT_REMINDER_CANCELLED, _utc(self.clock()),
                                    {"summary": f"reminder cancelled: {reason}", "event_id": event_id})
            self.on_event(f"pending not-live reminder {event_id} cancelled: {reason}")
        if out.episode_ended and not out.episode_started:
            self.on_event(f"offline episode ended: {out.episode_ended}")
        if out.due is not None:
            self._send_reminder(out.due)

    def _send_reminder(self, due: ReminderDue) -> None:
        act = self.cfg.activity
        now = self.clock()
        eid = _event_id("REM", now)
        frame = self.frame_cache.fresh(act.fresh_screenshot_max_age_seconds)
        shot = self._activity_shot(eid, frame)
        payload = format_not_live_reminder(
            self.cfg.machine_label, now, act.offline_threshold_minutes, due.accumulated_seconds, due.episode_id,
            due.sequence, act.repeat_max_count, bool(shot), self.live_rules.verified,
        )
        details = {"summary": f"not-live reminder {due.sequence} after {int(due.accumulated_seconds)} s confirmed offline",
                   "sequence": due.sequence, "offline_seconds": due.accumulated_seconds, "screenshot": bool(shot)}
        n = self.reminders.enqueue_reminder(due, payload, shot, eid, details, self.registry.targets(CAT_REMINDERS),
                                            CAT_REMINDERS)
        self.on_event(f"TIME TO GO LIVE reminder {due.sequence} -> {eid} queued for {n} bot(s)")
        self._kick()

    def _emit_activity(self) -> None:
        st = self.sessions.state
        bs = self.broadcast.state
        rs = self.reminders.state
        lc = self.broadcast.last_classification
        self.activity = ActivitySnapshot(
            app_state=st.app_state, session_id=self.sessions.session_id,
            live_state=bs.state.value, live_evidence=bs.evidence, live_rules_verified=self.live_rules.verified,
            last_confirmed_utc=bs.last_confirmed_utc, last_observation=lc.summary() if lc else "",
            offline_seconds=rs.accumulated_seconds, remaining_seconds=self.reminders.remaining_seconds(),
            accumulating=self.reminders.accumulating, episode_id=rs.episode_id, reminders_sent=rs.reminders_sent,
            last_event=st.last_event, delivery=self.queue.delivery_status(),
        )
        self.on_activity(self.activity)

    def _maybe_purge(self) -> None:
        now = self.clock()
        if now - self._last_purge < 3600:
            return
        self._last_purge = now
        dead = self.queue.expire_stale(self.cfg.telegram.delivery_max_age_hours * 3600)
        if dead:
            self.on_event(f"{dead} delivery(ies) dead-lettered after {self.cfg.telegram.delivery_max_age_hours:g} h")
        keep = self.queue.evidence_in_use()   # evidence stays while any delivery still needs it
        removed = purge_old_screenshots(self.cfg.screenshots_dir, self.cfg.privacy, now, keep)
        removed += purge_old_screenshots(self.cfg.activity_screenshots_dir, self.cfg.privacy, now, keep)
        if self.frame_cache.purge(self.cfg.privacy.screenshot_retention_days, now):
            removed += 1
        self.queue.purge_sent(30 * 86400)
        if removed:
            self.on_event(f"privacy: removed {removed} screenshot(s) past retention")

    # ------------------------------------------------------------------
    def run(self) -> None:
        """Blocking loop until :meth:`stop` is called."""
        if self.worker and not self.worker.is_alive():
            self.worker.start()
        self.on_event("monitoring started (Studio activity is only observed while the monitor runs)")
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
            try:
                self.frame_cache.flush()
            except OSError:  # pragma: no cover
                log.warning("could not persist latest frame")
            self._emit_status(Status.STOPPED, "")
            self.on_event("monitoring stopped")

    def stop(self) -> None:
        self._stop.set()
        if self.worker:
            self.worker.stop()
        try:
            self.registry.listeners.remove(self._registry_changed)
        except ValueError:
            pass

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="studio-monitor", daemon=True)
        thread.start()
        return thread


def _utc(ts: float) -> str:
    from datetime import timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def ensure_dirs(cfg: AppConfig) -> Path:
    cfg.data_path.mkdir(parents=True, exist_ok=True)
    cfg.screenshots_dir.mkdir(parents=True, exist_ok=True)
    cfg.activity_screenshots_dir.mkdir(parents=True, exist_ok=True)
    cfg.frame_cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg.data_path
