"""Monitoring loop: consume frames of the bound Studio window, OCR, match
rules, de-duplicate, and queue Telegram alerts.

Also drives Studio activity (session opened/closed, latest-frame cache,
broadcast-state engine, broadcast-start events, not-live reminders) and the
health model. Every notification becomes one event with immutable redacted
evidence and one delivery per enabled, subscribed bot (see :mod:`queue`).
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .account import (DISABLED_STATUS, FAILED, IN_PROGRESS, NOT_ATTEMPTED, SUCCEEDED, AccountIdentity, AccountLookupJob,
                      IdentityStore, Interactor, LookupContext, LookupResult, Win32Interactor)
from .detectors.suite import CATEGORY_OF as STREAM_CATEGORY_OF, DetectorSuite
from .alerts import (format_stream_alert, format_stream_recovered, format_duration, local_ts)
from .alerts import (format_alert, format_already_live, format_broadcast_started, format_health_alert,
                     format_not_live_reminder, format_studio_already_running, format_studio_closed,
                     format_studio_opened)
from .bots import CAT_STREAM
from .bots import (CAT_BROADCAST, CAT_HEALTH, CAT_REMINDERS, CAT_RESTRICTIONS, CAT_STUDIO_CLOSED, CAT_STUDIO_OPENED,
                   CAT_VERIFICATION, BotRegistry)
from .broadcast import BroadcastStateEngine, Classification, LiveRules, LiveState
from .broadcast_events import BroadcastEpisodeTracker
from .config import AppConfig, TargetIdentity
from .detection.detector import Detection, Detector
from .detection.rules import RuleSet
from .framecache import FrameCache
from .health import HealthAlertPolicy, HealthSnapshot
from .incidents import Incident, IncidentTracker
from .incident_engine import IncidentEngine
from .contracts.events import Event, EvidenceRef, Severity
from .hub_sync import HubSync
from .commands import CommandRouter, UpdatePoller, incident_keyboard
from .email_backup import EmailBackup
from .memory import MemoryProvider, format_hits, incident_doc, session_doc
from .pc_health import PcHealthSampler
from .clips import ClipBuffer
from .engagement import EngagementStats, parse_engagement
from .watchdog import StallDetector
from .alerts import format_pc_health_alert
from .session_report import build_report
from .ocr.base import OcrBackend, OcrError
from .privacy import purge_old_screenshots, redact
from .queue import KIND_ACTIVITY, KIND_INCIDENT, KIND_REMINDER, KIND_STATUS, Delivery, DeliveryError, DeliveryQueue, DeliveryWorker
from .reminders import EVT_REMINDER_CANCELLED, OfflineReminderEngine, ReminderDue
from .sessions import EVT_ALREADY_RUNNING, EVT_CLOSED, EVT_OPENED, SessionEvent, StudioSessionTracker
from .target import related_windows
from .telegram import ClientFactory, deliver
from .tracker import Status, WindowTracker
from .win32.capture import Capture, Capturer, CaptureStatus, FrameService, SyncFrameService, is_blank
from .win32.windows import WindowSystem

log = logging.getLogger(__name__)

EVT_BROADCAST_STARTED = "BROADCAST_STARTED"
EVT_ALREADY_LIVE = "BROADCAST_ALREADY_LIVE"
EVT_BROADCAST_ENDED = "BROADCAST_ENDED"


@dataclass
class StatusUpdate:
    status: Status
    reason: str
    window_title: str = ""
    queue_counts: Optional[dict] = None


@dataclass
class ActivitySnapshot:
    """What the GUI shows: Studio activity, capture health, broadcast state."""
    app_state: str = "NOT_RUNNING"
    session_id: str = ""
    live_state: str = LiveState.UNKNOWN.value
    live_evidence: str = ""
    live_rules_verified: bool = False
    last_confirmed_utc: str = ""
    last_observation: str = ""
    last_transition: str = ""
    broadcast_episode: str = ""
    offline_seconds: float = 0.0
    remaining_seconds: Optional[float] = None
    accumulating: bool = False
    episode_id: str = ""
    reminders_sent: int = 0
    last_event: str = ""
    delivery: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    health: HealthSnapshot = field(default_factory=HealthSnapshot)
    capture: CaptureStatus = field(default_factory=CaptureStatus)
    target_title: str = ""
    target_hwnd: int = 0
    stream: dict = field(default_factory=dict)      # condition -> {"state", "detail", "since"}
    stream_end_hint: str = ""
    hub: dict = field(default_factory=dict)         # HubStatus.to_dict() when a hub is configured


def _event_id(prefix: str, ts: float) -> str:
    return f"{prefix}-{datetime.fromtimestamp(ts):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def incident_category(popup_category: str) -> str:
    return CAT_VERIFICATION if popup_category == "verification_puzzle" else CAT_RESTRICTIONS


def incident_severity(popup_category: str) -> str:
    """Configurable defaults: verification and hard restrictions are urgent."""
    if popup_category in ("verification_puzzle", "account_suspension", "live_interruption", "restriction_notice"):
        return Severity.URGENT
    return Severity.WARNING


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
                 on_activity: Optional[Callable[[ActivitySnapshot], None]] = None,
                 frame_service: Optional[FrameService] = None,
                 owns_frame_service: bool = False,
                 interactor: Optional[Interactor] = None,
                 inline_lookup: bool = False,
                 lookup_sleep: Callable[[float], None] = time.sleep,
                 detector_suite: Optional[DetectorSuite] = None,
                 hub_sync: Optional[HubSync] = None,
                 command_poller: Optional[UpdatePoller] = None,
                 email_backup: Optional[EmailBackup] = None,
                 memory: Optional[MemoryProvider] = None,
                 pc_health: Optional[PcHealthSampler] = None,
                 clips: Optional[ClipBuffer] = None) -> None:
        self.cfg = cfg
        self.system = system
        self.capturer = capturer                 # dialogs only (PrintWindow, no desktop fallback)
        self.frames: FrameService = frame_service or SyncFrameService(system, capturer, cfg.capture.max_frame_age_seconds,
                                                                      clock, mono)
        self._owns_frames = owns_frame_service or frame_service is None
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
        self.episodes = BroadcastEpisodeTracker(queue, act.max_observation_gap_seconds, clock, mono)
        self.reminders = OfflineReminderEngine(
            queue, threshold_seconds=act.offline_threshold_minutes * 60.0,
            max_gap_seconds=act.max_observation_gap_seconds, repeat_enabled=act.repeat_enabled,
            repeat_interval_seconds=act.repeat_interval_minutes * 60.0, repeat_max=act.repeat_max_count,
            enabled=act.reminders_enabled, clock=clock, mono=mono,
        )
        self.health_policy = HealthAlertPolicy(queue, degrade_after=cfg.health.degrade_after_seconds,
                                               recover_after=cfg.health.recover_after_seconds, clock=clock, mono=mono)
        self.health = HealthSnapshot()
        self._ocr_failures = 0
        # durable incident engine (lifecycle / ack / snooze / maintenance / escalation) on the same SQLite file
        cfg.ensure_device_id()
        self.device_id = cfg.device.device_id
        self.incident_engine = IncidentEngine(queue._conn, queue._lock, clock)
        self._missed_start_window: Optional[str] = None
        self.worker: Optional[DeliveryWorker] = None
        if client_factory is not None:
            self.worker = DeliveryWorker(queue, self.send_delivery, cfg.telegram.delivery_concurrency,
                                         on_event=self.on_event)
            registry.listeners.append(self._registry_changed)
        self._stop = threading.Event()
        self._last_status: tuple[Status, str] = (Status.STOPPED, "")
        self._last_purge = 0.0
        self._last_seq = -1
        self.last_detections: list[Detection] = []
        self.last_transition = ""
        # automatic account (@username) discovery
        self.identity_store = IdentityStore(queue)
        self.account: AccountIdentity = self.identity_store.load()
        self.interactor: Optional[Interactor] = interactor
        if self.interactor is None and client_factory is not None:
            self.interactor = Win32Interactor(system)
        self._inline_lookup = inline_lookup
        self._lookup_sleep = lookup_sleep
        self._lookup: Optional[AccountLookupJob] = None
        self._pending_broadcast: Optional[dict] = None
        self._blocking_detection = False
        # stream-health detectors (connection / source / presenter / audio), evaluated only while LIVE
        self.detectors: Optional[DetectorSuite] = detector_suite
        # fleet hub mirror (events -> hub outbox; heartbeats carry the status payload)
        self._hub_sync: Optional[HubSync] = None
        self.hub_sync = hub_sync
        # Telegram commands (standalone mode) + e-mail backup route
        self.command_poller: Optional[UpdatePoller] = command_poller
        self.email_backup: Optional[EmailBackup] = email_backup
        self.memory: Optional[MemoryProvider] = memory
        self.pc_health: Optional[PcHealthSampler] = pc_health
        self._pc_incidents: dict[str, dict] = {}
        self.clips: Optional[ClipBuffer] = clips
        self.engagement = EngagementStats()
        self._last_live_text = ""
        self.stall = StallDetector(cfg.pc_health.stall_after_seconds, mono)
        self._stalled = False
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stream_episode_counts: dict[str, int] = {}
        self._session_started_utc = ""
        self.last_report: Optional[dict] = None
        self._last_failed_delivery_id = int(queue.get_state("email_backup_last_delivery_id", 0) or 0)
        self._started_mono = mono()
        self._stream_incidents: dict[str, dict] = {}
        self._stream_end_hint = ""
        self.activity = ActivitySnapshot(live_rules_verified=self.live_rules.verified)
        if cfg.target.hwnd:
            self.frames.bind(cfg.target.hwnd)

    # ------------------------------------------------------------------
    def _identity_changed(self, identity: TargetIdentity) -> None:
        self.cfg.target = identity
        self.frames.bind(identity.hwnd)
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
        thread_of = d.payload.get("thread_of") or ""
        reply_to = self.incident_engine.root_message(thread_of, d.bot_id, d.chat_id) if thread_of else None
        result = deliver(client, d.payload, d.evidence_path, self.clock, reply_to=reply_to)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id and not thread_of and d.event_id.startswith("INC-"):
            # first message of an incident: remember the root per bot/destination for threaded follow-ups
            self.incident_engine.set_root_message(d.event_id, d.bot_id, d.chat_id, d.thread_id, int(message_id))
        return message_id

    def dispatch(self, event_id: str, kind: str, category: str, payload: dict, evidence_path: str,
                 label: str = "", incident_id: str = "", event_type: str = "", extra_detail: Optional[dict] = None) -> int:
        """Create one event + one delivery per enabled subscribed bot. Deliveries
        are withheld (the event is still recorded) while the category is
        suppressed by maintenance or a snooze, or when a hub owns delivery.
        Every event is also mirrored to the hub outbox when a hub is configured."""
        suppressed = self.incident_engine.is_suppressed(self.device_id, category, incident_id)
        managed = self.cfg.device.mode == "managed"
        targets = [] if (suppressed or managed) else self.registry.targets(category)
        n = self.queue.create_event(event_id, kind, category, payload, evidence_path, targets, label,
                                    owner_label=self.cfg.notification_label)
        if suppressed:
            self.on_event(f"{label or event_id}: recorded but not delivered (maintenance/snooze active for {category})")
        self.mirror_to_hub(event_id, kind, category, payload, evidence_path, label, incident_id, event_type, extra_detail)
        self._kick()
        return n

    # ---- fleet hub ----------------------------------------------------
    @property
    def hub_sync(self) -> Optional[HubSync]:
        return self._hub_sync

    @hub_sync.setter
    def hub_sync(self, sync: Optional[HubSync]) -> None:
        """Wire the status payload and the predefined remote-operation handler whenever a sync is attached."""
        self._hub_sync = sync
        if sync is not None:
            sync.status_provider = self.hub_status_payload
            sync.on_command = self.execute_remote_command

    POPUP_EVENT_TYPES = {"restriction_notice": "RESTRICTION", "content_warning": "CONTENT_WARNING",
                         "account_suspension": "ACCOUNT_SUSPENSION", "live_interruption": "LIVE_INTERRUPTED",
                         "verification_puzzle": "VERIFICATION"}
    HUB_NAMESPACE = uuid.UUID("6f1b9b3e-6d55-4a0e-9d2b-0a1b2c3d4e5f")

    def hub_event_id(self, local_event_id: str) -> str:
        """Deterministic UUID5 of the local event id: re-dispatch -> same id -> hub dedup."""
        return str(uuid.uuid5(self.HUB_NAMESPACE, f"{self.device_id}:{local_event_id}"))

    @staticmethod
    def _plain(text: str) -> str:
        import html as _html
        import re as _re
        return _html.unescape(_re.sub(r"<[^>]+>", "", text or "")).strip()

    def _guess_event_type(self, kind: str, category: str, event_id: str, label: str) -> str:
        if event_id.endswith("-RES"):
            return "INCIDENT_RESOLVED"
        if "-E" in event_id.rsplit("-", 1)[-1] and event_id.rsplit("-", 1)[-1][1:].isdigit():
            return "INCIDENT_ESCALATION"
        if event_id.startswith("SCH"):
            return "SCHEDULE_MISSED_START"
        if event_id.startswith("HLT"):
            return "HEALTH_RECOVERED" if "recovered" in label.lower() else "HEALTH_DEGRADED"
        if event_id.startswith("REM"):
            return "NOT_LIVE_REMINDER"
        if event_id.startswith("TEST") or category == "test":
            return "TEST"
        return {CAT_STUDIO_OPENED: "STUDIO_ALREADY_RUNNING" if "already" in label.lower() else "STUDIO_OPENED",
                CAT_STUDIO_CLOSED: "STUDIO_CLOSED",
                CAT_BROADCAST: "BROADCAST_ALREADY_LIVE" if "already" in label.lower() else "BROADCAST_STARTED",
                CAT_REMINDERS: "NOT_LIVE_REMINDER", CAT_HEALTH: "HEALTH_DEGRADED", CAT_VERIFICATION: "VERIFICATION",
                CAT_RESTRICTIONS: "RESTRICTION"}.get(category, "TEST")

    def mirror_to_hub(self, event_id: str, kind: str, category: str, payload: dict, evidence_path: str, label: str = "",
                      incident_id: str = "", event_type: str = "", extra_detail: Optional[dict] = None) -> bool:
        if self.hub_sync is None:
            return False
        etype = event_type or self._guess_event_type(kind, category, event_id, label)
        text = payload.get("text") or payload.get("caption") or label
        summary = (label + ": " if label and label not in text else "") + self._plain(text).split("\n", 1)[0]
        attach = bool(evidence_path) and self.cfg.privacy.send_screenshots and self.cfg.hub.upload_evidence
        evidence = EvidenceRef.from_file(evidence_path, _utc(self.clock())) if attach else EvidenceRef()
        ev = Event(device_id=self.device_id, type=etype, summary=summary[:1000], event_id=self.hub_event_id(event_id),
                   session_id=self.sessions.session_id, incident_id=incident_id or (event_id if kind == KIND_INCIDENT else ""),
                   category=category, observed_utc=_utc(payload.get("created_at") or self.clock()),
                   owner_label=self.cfg.notification_label, account=self.current_account_handle(),
                   account_status=self.account.status, expected_account=self.cfg.device.expected_account,
                   detail={"local_event_id": event_id, "kind": kind, "label": label, "thread_of": payload.get("thread_of", ""),
                           "mode": self.cfg.device.mode, **(extra_detail or {})},
                   evidence=evidence,
                   payload={k: payload[k] for k in ("text", "caption", "created_at", "thread_of") if k in payload})
        return self.hub_sync.outbox.enqueue(ev.to_dict(), evidence.path, evidence.sha256)

    def hub_status_payload(self) -> dict:
        a = self.activity
        problems = [k for k, v in (a.stream or {}).items() if v.get("state") == "PROBLEM"]
        return {"live_state": a.live_state, "app_state": a.app_state, "capture": a.health.capture, "ocr": a.health.ocr,
                "delivery": a.health.delivery, "account": self.current_account_handle(), "account_status": self.account.status,
                "mode": self.cfg.device.mode, "owner_label": self.cfg.notification_label, "session_id": self.sessions.session_id,
                "episode_id": self.episodes.state.episode_id, "pending": (a.delivery or {}).get("pending", 0),
                "stream_problems": problems, "version": __import__("studio_monitor").__version__,
                "target_title": a.target_title, "pc_health": self.pc_health.snapshot() if self.pc_health else {},
                "engagement": self.engagement.to_dict(), "stalled": self._stalled}

    def _emit_status(self, status: Status, reason: str) -> None:
        title = self.tracker.state.window.title if self.tracker.state.window else ""
        self.on_status(StatusUpdate(status, reason, title, self.queue.counts()))
        self._last_status = (status, reason)

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
        if state.window is not None and self.frames.status().hwnd != state.window.hwnd:
            self.frames.bind(state.window.hwnd)   # follow the validated window (recreation / restart)

        captures: list[Capture] = []
        detections: list[Detection] = []
        main_cap: Optional[Capture] = None
        main_full_text: Optional[str] = None
        fresh_frame = False
        if state.status in (Status.RUNNING, Status.DEGRADED) and state.window is not None:
            main = state.window
            cap = self.frames.frame()
            if cap is not None and cap.hwnd == main.hwnd:
                fresh_frame = cap.seq != self._last_seq
                self._last_seq = cap.seq
                # work on a copy: the service's frame stays immutable
                cap = Capture(cap.image.copy(), main, cap.method, cap.reliable, cap.note, False,
                              cap.captured_at, cap.captured_mono, cap.hwnd, cap.seq)
                captures.append(cap)
                main_cap = cap
            if self.cfg.detection.include_dialogs and state.status == Status.RUNNING:
                foreground = self.system.foreground_window()
                for win in related_windows(self.system, main):
                    dcap = self.capturer.capture(win, foreground)
                    if dcap is not None and not is_blank(dcap.image):
                        dcap.is_dialog = True
                        captures.append(dcap)
            for ev in self.tracker.drain_events():
                self.on_event(ev)
            if captures:
                self.on_capture(captures[0])

            if fresh_frame or any(c.is_dialog for c in captures):
                for c in captures:
                    if c is main_cap and not fresh_frame:
                        continue
                    # Redaction happens before OCR so redacted areas are never read, stored or sent.
                    c.image = redact(c.image, self.cfg.regions)
                    regions = [] if c.is_dialog else [r for r in self.cfg.regions if r.kind == "detect"]
                    try:
                        det = self.detector.detect(c, regions)
                        self._ocr_failures = 0
                    except OcrError as exc:
                        self._ocr_failures += 1
                        self.health.ocr, self.health.ocr_reason = "FAILING", str(exc)[:120]
                        det = None
                    if c is main_cap:
                        main_full_text = self.detector.last_full_text
                    if det is not None:
                        detections.append(det)
            elif main_cap is not None:
                main_cap.image = redact(main_cap.image, self.cfg.regions)

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
        for gone in self.incidents.newly_gone:
            self._resolve_popup_incident(gone)
        self._escalate_due()
        self.last_detections = detections
        self._blocking_detection = bool(detections)
        if self._ocr_failures == 0:
            self.health.ocr, self.health.ocr_reason = "OK", ""

        # ---- Studio activity: session, frame cache, broadcast state, reminders
        screenshot_ok = main_cap is not None
        if screenshot_ok and fresh_frame:
            self.frame_cache.update(main_cap.image)
            if self.clips is not None:
                self.clips.add(main_cap.image)
        self._update_sessions(window_present, screenshot_ok)
        popup_on_main = any(not d.is_dialog for d in detections)
        classification: Optional[Classification] = None
        if screenshot_ok and fresh_frame and not popup_on_main and self.sessions.running:
            classification = self._classify_live(main_cap, main_full_text)
        self._update_broadcast_and_reminders(classification, main_cap if fresh_frame else None)
        self._poll_lookup()
        self._run_detectors(main_cap, fresh_frame and screenshot_ok, main_full_text)

        self._update_health(state)
        self._emit_status(self.tracker.state.status, self.tracker.state.reason)
        self._emit_activity()
        if self.hub_sync is not None and self.hub_sync._thread is None:
            self.hub_sync.tick()                       # inline mode (tests / --once); production runs a thread
        if self.command_poller is not None and self.command_poller._thread is None:
            self.command_poller.poll_once(timeout=0)
        self._check_email_backup()
        self._observe_engagement(fresh_frame and screenshot_ok)
        self._run_pc_health()
        self.stall.beat()
        self._maybe_purge()
        return detections

    # ---- stream-health detectors --------------------------------------
    STREAM_CODES = {"RECONNECTING": "CON", "SOURCE_MISSING": "SRC", "BLACK_PREVIEW": "BLK", "FACE_ABSENT": "FAC",
                    "FACE_MOTION_LOW": "MOT", "PREVIEW_FROZEN": "FRZ", "AUDIO_SILENCE": "AUD"}
    STREAM_SEVERITY = {"RECONNECTING": Severity.WARNING, "SOURCE_MISSING": Severity.WARNING, "BLACK_PREVIEW": Severity.WARNING,
                       "FACE_ABSENT": Severity.WARNING, "FACE_MOTION_LOW": Severity.INFO, "PREVIEW_FROZEN": Severity.WARNING,
                       "AUDIO_SILENCE": Severity.WARNING}

    def _run_detectors(self, main_cap: Optional[Capture], fresh: bool, text: Optional[str]) -> None:
        suite = self.detectors
        if suite is None:
            return
        bs = self.broadcast.state.state
        ep = self.episodes.state
        # LIVE, or a transitional/unreadable screen (UNKNOWN) inside an open LIVE episode: reconnecting
        # overlays are exactly the moments the detectors exist for and never mean NOT_LIVE.
        live = self.sessions.running and (bs == LiveState.LIVE or
                                          (bs == LiveState.UNKNOWN and ep.last_confirmed == LiveState.LIVE.value and bool(ep.episode_id)))
        if not live:
            if self._stream_incidents:
                self._close_stream_incidents("Broadcast is no longer confirmed LIVE; stream-health evaluation stopped.")
            if suite.cond["RECONNECTING"].confirmed or any(c.confirmed for c in suite.cond.values()):
                suite.reset()
            self._stream_end_hint = ""
            self.activity.stream = {}
            return
        if self._lookup is not None:                       # profile-menu lookup changes the frame; pause briefly
            suite.suspend(max(2.0, self.cfg.detectors.scene_change_grace_seconds))
        suppressed = self.incident_engine.is_suppressed(self.device_id, CAT_STREAM) or self._blocking_detection
        frame = main_cap.image if (main_cap is not None and fresh) else None
        out = suite.evaluate(frame, fresh and frame is not None, True, text or "", self.cfg.regions, suppressed=suppressed)
        if fresh and text:
            ended = suite.classify_text(text).get("ended")
            if ended and not self._stream_end_hint:
                self._stream_end_hint = ended
                self.on_event(f"stream-end wording seen while LIVE ({ended!r}); broadcast state is decided by the live-state engine")
        for name, detail in out.confirmed:
            self._stream_episode_counts[name] = self._stream_episode_counts.get(name, 0) + 1
            self._open_stream_incident(name, detail, main_cap)
        for name, detail in out.recovered:
            self._resolve_stream_incident(name, detail)
        self.activity.stream = {k: {"state": c.state, "detail": c.detail, "since": c.since} for k, c in out.conditions.items()}

    def _open_stream_incident(self, name: str, detail: str, main_cap: Optional[Capture]) -> None:
        now = self.clock()
        cond = self.detectors.cond[name]
        since = now - cond.duration(self.mono())
        eid = _event_id("STR" + self.STREAM_CODES.get(name, "GEN"), now)
        shot = self._save_evidence(eid, main_cap.image if main_cap is not None else None)
        attach = bool(shot)
        payload = format_stream_alert(name, detail, self.cfg.machine_label, now, since, label=self.cfg.notification_label,
                                      account=self.current_account_handle(), screenshot_attached=attach,
                                      rules_verified=self.detectors.rules.verified, episodes=cond.episodes)
        change = self.incident_engine.open_or_update(
            self.device_id, CAT_STREAM, name, self.STREAM_SEVERITY.get(name, Severity.WARNING), detail,
            session_id=self.sessions.session_id, evidence_path=shot, account=self.current_account_handle(),
            owner_label=self.cfg.notification_label, incident_id=eid)
        self._stream_incidents[name] = {"incident_id": change.incident.incident_id, "since": since, "event_id": eid}
        if not change.is_new:
            payload["thread_of"] = change.incident.incident_id
        n = self.dispatch(eid, KIND_INCIDENT, CAT_STREAM, payload, shot, label=f"Stream {name}", incident_id=change.incident.incident_id,
                          event_type=name if name in ("FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN", "SOURCE_MISSING", "BLACK_PREVIEW", "AUDIO_SILENCE") else "BROADCAST_RECONNECTING")
        self.on_event(f"stream health: {name} confirmed -> {eid} queued for {n} bot(s): {detail}")

    def _resolve_stream_incident(self, name: str, detail: str, final: bool = False) -> None:
        info = self._stream_incidents.pop(name, None)
        if info is None:
            return
        now = self.clock()
        self.incident_engine.resolve(info["incident_id"], detail, observed_utc=_utc(now))
        self.remember_incident(info["incident_id"])
        if final:
            self.on_event(f"stream health: {name} closed ({detail})")
            return
        payload = format_stream_recovered(name, detail, self.cfg.machine_label, now, now - info["since"],
                                          label=self.cfg.notification_label)
        payload["thread_of"] = info["incident_id"]
        self.dispatch(f"{info['event_id']}-RES", KIND_INCIDENT, CAT_STREAM, payload, "", label=f"Stream {name} cleared",
                      incident_id=info["incident_id"], event_type="BROADCAST_RECONNECTED" if name == "RECONNECTING" else "INCIDENT_RESOLVED")
        self.on_event(f"stream health: {name} cleared -> {info['event_id']}-RES queued")

    def _close_stream_incidents(self, reason: str) -> None:
        for name in list(self._stream_incidents):
            self._resolve_stream_incident(name, reason, final=True)

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
        clip_path = ""
        if self.clips is not None and attach:
            clip_path = self.clips.write(shots / f"{inc.incident_id}.gif") or ""
        payload = format_alert(
            inc, self.cfg.machine_label, self.cfg.privacy.max_text_in_alert,
            screenshot_attached=attach, reason=reason if reason != "new incident" else "",
            capture_method=det.capture.method, label=self.cfg.notification_label,
            account=self.current_account_handle(),
        )
        payload["created_at"] = self.clock()
        if self.cfg.commands.enabled and self.cfg.commands.buttons:
            payload["buttons"] = incident_keyboard(inc.incident_id)
        if clip_path and self.cfg.clips.send:
            payload["clip_path"] = clip_path
            payload["clip_caption"] = f"Clip: the {self.cfg.clips.seconds_before:.0f} s before this alert (redacted frames)."
        stored_text = inc.text if self.cfg.privacy.store_detected_text else ""
        self.queue.record_incident(inc.incident_id, inc.category, inc.label, stored_text,
                                   inc.window_title, inc.is_dialog, inc.screenshot_path)
        event_id = inc.incident_id if inc.alerts_sent <= 1 else f"{inc.incident_id}-R{inc.alerts_sent}"
        category = incident_category(inc.category)
        change = self.incident_engine.open_or_update(
            self.device_id, category, inc.category, incident_severity(inc.category), inc.label + ": " + inc.text[:200],
            session_id=self.sessions.session_id, evidence_path=inc.screenshot_path, account=self.current_account_handle(),
            owner_label=self.cfg.notification_label, incident_id=inc.incident_id)
        if inc.alerts_sent > 1:
            payload["thread_of"] = change.incident.incident_id
        n = self.dispatch(event_id, KIND_INCIDENT, category, payload, inc.screenshot_path if attach else "",
                          label=inc.label, incident_id=change.incident.incident_id,
                          event_type=self.POPUP_EVENT_TYPES.get(inc.category, "RESTRICTION"))
        self.on_event(
            f"{'MANUAL ATTENTION: ' if inc.manual_attention else ''}{inc.label} detected in "
            f"{'dialog' if inc.is_dialog else 'main window'} -> {event_id} queued for {n} bot(s) ({reason})"
        )

    def _resolve_popup_incident(self, inc: Incident) -> None:
        """The popup is no longer visible. That is all the evidence says."""
        eng_inc = self.incident_engine.get(inc.incident_id)
        if eng_inc is None or not eng_inc.is_open:
            return
        text = f"{inc.label} no longer visible in Studio (this does not confirm the restriction was lifted)"
        self.incident_engine.resolve(inc.incident_id, text, observed_utc=_utc(self.clock()))
        self.remember_incident(inc.incident_id)
        payload = {"text": f"\u2705 <b>{self.cfg.notification_label} \u2014 RESOLVED</b>\n{text}\nIncident <code>{inc.incident_id}</code>\n"
                           f"Time: {datetime.fromtimestamp(self.clock()):%Y-%m-%d %H:%M:%S}", "created_at": self.clock(),
                   "thread_of": inc.incident_id}
        payload["caption"] = payload["text"]
        self.dispatch(f"{inc.incident_id}-RES", KIND_INCIDENT, incident_category(inc.category), payload, "",
                      label=f"{inc.label} resolved", incident_id=inc.incident_id, event_type="INCIDENT_RESOLVED")
        self.on_event(f"incident {inc.incident_id} resolved: {text}")

    def _escalate_due(self) -> None:
        for due in self.incident_engine.escalations_due():
            claimed = self.incident_engine.claim_escalation(due.incident.incident_id, due.claim_seq)
            if claimed is None:
                continue
            inc = claimed
            text = (f"\u23F0 <b>{self.cfg.notification_label} \u2014 STILL OPEN</b>\n{inc.summary}\n"
                    f"Reminder {inc.reminders_sent}; open since {inc.opened_utc}. Reply /ack {inc.incident_id} when handled.")
            payload = {"text": text, "caption": text, "created_at": self.clock(), "thread_of": inc.incident_id}
            if self.cfg.commands.enabled and self.cfg.commands.buttons:
                payload["buttons"] = incident_keyboard(inc.incident_id, with_screenshot=False)
            self.dispatch(f"{inc.incident_id}-E{inc.reminders_sent}", KIND_INCIDENT, inc.category, payload, inc.evidence_path,
                          label=f"Escalation {inc.reminders_sent}", incident_id=inc.incident_id, event_type="INCIDENT_ESCALATION")
            self.on_event(f"escalation reminder {inc.reminders_sent} for {inc.incident_id} queued")
            self._escalation_route(inc, payload)

    def _escalation_route(self, inc, payload: dict) -> None:
        """Once an incident stayed unacknowledged through N reminders, also notify the escalation destination."""
        esc = self.cfg.escalation
        if not (esc.enabled and esc.chat_id) or inc.reminders_sent < esc.after_reminders or self.cfg.device.mode == "managed":
            return
        from .bots import BotTarget
        bot_id = esc.bot_id or next((b.bot_id for b in self.cfg.bots if b.enabled), "")
        if not bot_id:
            return
        bot = next((b for b in self.cfg.bots if b.bot_id == bot_id), None)
        if bot is None or not bot.enabled:
            return
        esc_text = (f"\U0001F6A8 <b>{self.cfg.notification_label} \u2014 ESCALATION</b>\n{inc.summary}\n"
                    f"Unacknowledged after {inc.reminders_sent} reminder(s); open since {inc.opened_utc}. "
                    f"Incident <code>{inc.incident_id}</code>.")
        esc_payload = {"text": esc_text, "caption": esc_text, "created_at": self.clock()}
        eid = f"{inc.incident_id}-X{inc.reminders_sent}"
        self.queue.create_event(eid, KIND_INCIDENT, inc.category, esc_payload, inc.evidence_path,
                                [BotTarget(bot.bot_id, bot.name, esc.chat_id, esc.thread_id)], f"Escalation route {inc.reminders_sent}",
                                owner_label=self.cfg.notification_label)
        self.on_event(f"escalation route notified for {inc.incident_id} (chat {esc.chat_id})")
        self._kick()

    # ---- e-mail backup --------------------------------------------------
    def _check_email_backup(self) -> None:
        eb = self.email_backup
        if eb is None or not eb.configured:
            return
        rank = {Severity.INFO: 0, Severity.WARNING: 1, Severity.URGENT: 2}
        minimum = rank.get(self.cfg.smtp.min_severity, 2)
        for f in self.queue.failed_deliveries_since(self._last_failed_delivery_id):
            self._last_failed_delivery_id = f["id"]
            sev = Severity.INFO
            if f["kind"] == KIND_INCIDENT:
                inc_id = f["event_id"].split("-RES")[0]
                inc = self.incident_engine.get(inc_id) if inc_id.startswith("INC") or inc_id.startswith("STR") else None
                sev = inc.severity if inc is not None else Severity.URGENT
            if rank.get(sev, 0) < minimum:
                continue
            body = (f"{self.cfg.notification_label}: Telegram delivery to '{f['bot_name']}' failed for {f['label'] or f['event_id']}.\n"
                    f"Last error: {f['error']}\n\n{self._plain(f['payload'].get('text') or f['payload'].get('caption') or '')}\n\n"
                    f"PC: {self.cfg.machine_label}  Event: {f['event_id']}")
            ok = eb.send(f"[{self.cfg.notification_label}] {f['label'] or f['event_id']} (Telegram failed)", body)
            self.on_event(f"e-mail backup {'sent' if ok else 'failed: ' + eb.last_error} for {f['event_id']}")
        self.queue.set_state("email_backup_last_delivery_id", self._last_failed_delivery_id)

    # ---- reports + memory ------------------------------------------------
    def _emit_report(self, kind: str, episode_id: str, ended_at: float, session_id: str = "") -> Optional[dict]:
        if not self.cfg.activity.session_reports:
            return None
        session_id = session_id or self.sessions.session_id
        summary = self.incident_engine.session_summary(self.device_id, session_id) if session_id else {"incidents": 0, "by_category": {}}
        ep = self.episodes.state
        rs = self.reminders.state
        a = self.account
        started = ep.live_since_utc if kind == "broadcast_report" else self._session_started_utc
        report = build_report(kind, device_id=self.device_id, device_name=self.cfg.device.device_name or self.cfg.machine_label,
                              owner_label=self.cfg.notification_label, session_id=session_id, episode_id=episode_id,
                              started_utc=started or "", ended_at=ended_at, summary=summary, reminders_sent=rs.reminders_sent,
                              offline_seconds=rs.accumulated_seconds, stream_problems=dict(self._stream_episode_counts),
                              account=a.handle if a.status == SUCCEEDED else "", account_status=a.status,
                              live_rules_verified=self.live_rules.verified,
                              note=("Engagement (Studio counters): " + self.engagement.summary()) if self.engagement.summary() else "")
        report["engagement"] = self.engagement.to_dict()
        self.last_report = report
        eid = _event_id("RPT", ended_at)
        payload = {"text": report["text_html"], "caption": report["text_html"], "created_at": ended_at}
        category = CAT_BROADCAST if kind == "broadcast_report" else CAT_STUDIO_CLOSED
        self.queue.record_event(eid, "SESSION_REPORT", _utc(ended_at), {"summary": report["text_plain"], "report": report},
                                session_id, episode_id)
        if kind == "session_report" and not self.cfg.activity.notify_closed:
            # Studio-closed notifications are off: keep the report in history/hub/memory, send nothing
            self.mirror_to_hub(eid, KIND_ACTIVITY, category, payload, "", label="Session Report", event_type="SESSION_REPORT",
                               extra_detail={"report": report})
            n = 0
        else:
            n = self.dispatch(eid, KIND_ACTIVITY, category, payload, "", label=report["kind"].replace("_", " ").title(),
                              event_type="BROADCAST_ENDED" if kind == "broadcast_report" else "SESSION_REPORT",
                              extra_detail={"report": report})
        self.on_event(f"{report['kind'].replace('_', ' ')} for {episode_id or session_id} -> {eid} queued for {n} bot(s)")
        if kind == "broadcast_report":
            self._stream_episode_counts = {}
        self._remember(session_doc(report))
        return report

    def _remember(self, doc) -> None:
        """Store a summary in long-term memory (never OCR-driven decisions; failures are logged, not fatal)."""
        if self.memory is None:
            return
        try:
            if self.memory.add(doc):
                self.on_event(f"memory: stored {doc.id}")
            else:
                self.on_event(f"memory: could not store {doc.id}: {getattr(self.memory, 'last_error', '') or 'unknown error'}")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("memory add failed: %s", exc)

    def remember_incident(self, incident_id: str) -> None:
        inc = self.incident_engine.get(incident_id)
        if inc is None or self.memory is None:
            return
        self._remember(incident_doc(inc, self.cfg.device.device_name or self.cfg.machine_label, self.cfg.notification_label))

    def similar_from_memory(self, query: str) -> str:
        if self.memory is None or not query:
            return ""
        hits = self.memory.search(query, {"device_id": self.device_id}, self.cfg.memory.retrieval_limit)
        return format_hits(hits, "Similar past incidents / sessions")

    # ---- engagement / PC health / watchdog --------------------------------
    def _observe_engagement(self, fresh: bool) -> None:
        ep = self.episodes.state.episode_id
        if self.engagement.episode_id != ep:
            self.engagement = EngagementStats(episode_id=ep)
        if not fresh or not ep or self.broadcast.state.state != LiveState.LIVE or not self._last_live_text:
            return
        reading = parse_engagement(self._last_live_text)
        if reading:
            self.engagement.observe(self.clock(), reading)

    def _run_pc_health(self) -> None:
        ph = self.pc_health
        if ph is None or not ph.due():
            return
        sample = ph.sample(self.tracker.identity.pid if self.sessions.running else 0)
        live = self.broadcast.state.state == LiveState.LIVE
        confirmed, recovered = ph.evaluate(sample, live)
        now = self.clock()
        for name, text in confirmed:
            eid = _event_id("PCH", now)
            payload = format_pc_health_alert("problem", name, text, self.cfg.machine_label, now, label=self.cfg.notification_label,
                                             sample=sample.to_dict())
            change = self.incident_engine.open_or_update(self.device_id, CAT_HEALTH, f"PC_{name}", Severity.WARNING, text,
                                                         session_id=self.sessions.session_id, owner_label=self.cfg.notification_label,
                                                         incident_id=eid)
            self._pc_incidents[name] = {"incident_id": change.incident.incident_id, "event_id": eid}
            if not self.registry.targets(CAT_HEALTH) and self.cfg.device.mode != "managed":
                self.on_event(f"PC health: {name} ({text}) recorded; no bot subscribed to health alerts")
            self.dispatch(eid, KIND_INCIDENT, CAT_HEALTH, payload, "", label=f"PC health {name}", incident_id=change.incident.incident_id,
                          event_type="PC_HEALTH", extra_detail={"condition": name, "sample": sample.to_dict()})
            self.on_event(f"PC health: {name} confirmed -> {eid}")
        for name, text in recovered:
            info = self._pc_incidents.pop(name, None)
            if info is None:
                continue
            self.incident_engine.resolve(info["incident_id"], text, observed_utc=_utc(now))
            payload = format_pc_health_alert("recovered", name, text, self.cfg.machine_label, now, label=self.cfg.notification_label)
            payload["thread_of"] = info["incident_id"]
            self.dispatch(f"{info['event_id']}-RES", KIND_INCIDENT, CAT_HEALTH, payload, "", label=f"PC health {name} ok",
                          incident_id=info["incident_id"], event_type="INCIDENT_RESOLVED")
            self.on_event(f"PC health: {name} recovered")

    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            gap = self.stall.check()
            if gap is not None:
                self._stalled = True
                log.error("monitor loop stalled for %.0f s", gap)
                try:
                    self.on_event(f"WATCHDOG: monitor loop has not completed a poll for {gap:.0f} s (capture/OCR may be blocked); "
                                  "heartbeats keep reporting 'stalled'")
                except Exception:  # pragma: no cover
                    pass
            elif self._stalled and self.stall.check() is None and self.mono() - self.stall._last < self.stall.stall_after:
                self._stalled = False
                self.on_event("WATCHDOG: monitor loop is polling again")
            self._stop.wait(10.0)

    # ---- predefined remote operations ---------------------------------
    REMOTE_OPS = ("screenshot", "status")

    def execute_remote_command(self, cmd: dict) -> None:
        """Only predefined operations; anything else is recorded and ignored."""
        op = str((cmd or {}).get("op", ""))
        if op not in self.REMOTE_OPS:
            self.on_event(f"remote command ignored (not a predefined operation): {op!r}")
            return
        if op == "screenshot":
            path, caption = self.command_screenshot()
            eid = _event_id("SHOT", self.clock())
            payload = {"text": caption, "caption": caption, "created_at": self.clock()}
            self.mirror_to_hub(eid, KIND_ACTIVITY, CAT_HEALTH, payload, path, label="Screenshot on request", event_type="SCREENSHOT")
            self.queue.record_event(eid, "SCREENSHOT", _utc(self.clock()), {"summary": "screenshot requested remotely",
                                                                           "requested_by": cmd.get("requested_by", "")},
                                    self.sessions.session_id, "", path)
            self.on_event("remote screenshot captured and mirrored to the hub" if path else "remote screenshot: no fresh frame")
        elif op == "status":
            self.on_event("remote status request answered via heartbeat")

    def command_screenshot(self, _device: str = "") -> tuple[Optional[str], str]:
        frame = self.frame_cache.fresh(self.cfg.activity.fresh_screenshot_max_age_seconds)
        if frame is None or not self.cfg.privacy.send_screenshots:
            reason = "screenshots disabled by privacy settings" if not self.cfg.privacy.send_screenshots else \
                ("Studio is not running" if not self.sessions.running else "no fresh frame (window minimized or capture degraded)")
            return None, f"No screenshot available: {reason}."
        dest = self.cfg.activity_screenshots_dir / f"CMD-{datetime.fromtimestamp(self.clock()):%Y%m%d-%H%M%S}.png"
        path = self.frame_cache.export(dest)
        if not path:
            return None, "No screenshot available: could not export the cached frame."
        return path, (f"\U0001F4F7 <b>{self.cfg.notification_label}</b> \u2014 Studio frame captured "
                      f"{datetime.fromtimestamp(frame.captured_at):%H:%M:%S} (redacted). Broadcast: {self.broadcast.state.state.value}.")

    # ---- Telegram command backend (standalone) --------------------------
    def command_status(self) -> str:
        import html as _html
        a = self.activity
        acct = self.account_snapshot()
        lines = [f"<b>{_html.escape(self.cfg.notification_label)} \u2014 status</b>",
                 f"Studio: {a.app_state.replace('_', ' ')}" + (f" (session {a.session_id})" if a.session_id else ""),
                 f"Broadcast: {a.live_state.replace('_', ' ')}" + ("" if a.live_rules_verified else " (rules unverified)"),
                 f"TikTok account: {_html.escape(acct.get('handle') or acct.get('display') or 'unknown')}",
                 f"Capture: {a.health.capture}" + (f" ({_html.escape(a.health.capture_reason)})" if a.health.capture_reason else ""),
                 f"OCR: {a.health.ocr} \u00b7 Delivery: {a.health.delivery}"]
        problems = [k.replace("_", " ").lower() for k, v in (a.stream or {}).items() if v.get("state") == "PROBLEM"]
        if problems:
            lines.append("Stream health: " + ", ".join(problems))
        open_incs = self.incident_engine.list(self.device_id, "OPEN", 10)
        lines.append(f"Open incidents: {len(open_incs)}")
        for inc in open_incs[:5]:
            lines.append(f"  \u2013 [{inc.severity}] <code>{_html.escape(inc.incident_id)}</code> {_html.escape(inc.summary[:70])}"
                         + (" (acked)" if inc.acknowledged else ""))
        if self.engagement.summary():
            lines.append("Engagement (Studio counters): " + _html.escape(self.engagement.summary()))
        if self.pc_health is not None and self.pc_health.last is not None:
            sm = self.pc_health.last
            lines.append(f"PC: CPU {sm.cpu_percent:.0f}% \u00b7 memory {sm.memory_percent:.0f}% \u00b7 disk free {sm.disk_free_percent:.0f}%"
                         + (f" \u00b7 problems: {', '.join(self.pc_health.snapshot()['problems'])}" if self.pc_health.snapshot()["problems"] else ""))
        if self.hub_sync is not None:
            hs = self.hub_sync.status
            lines.append(f"Hub: {'connected' if hs.connected else 'disconnected'} \u00b7 pending {hs.pending}")
        lines.append(f"Monitor up {format_duration(self.mono() - self._started_mono)} \u00b7 PC {_html.escape(self.cfg.machine_label)}")
        return "\n".join(lines)

    def command_sessions(self, limit: int = 10) -> str:
        import html as _html
        rows = self.queue.recent_events(limit * 3, ["STUDIO_OPENED", "STUDIO_ALREADY_RUNNING", "STUDIO_CLOSED",
                                                     EVT_BROADCAST_STARTED, EVT_ALREADY_LIVE, EVT_BROADCAST_ENDED])
        if not rows:
            return "No Studio sessions recorded yet."
        lines = ["<b>Recent sessions</b>"]
        for r in rows[:limit * 2]:
            d = r.get("details") or {}
            lines.append(f"{str(r.get('ts_utc', ''))[11:19]} UTC \u00b7 {r['event_type'].replace('_', ' ').title()}"
                         + (f" \u00b7 {_html.escape(str(d.get('account')))}" if d.get("account") else ""))
        return "\n".join(lines)

    def command_ack(self, incident_id: str, actor: str) -> str:
        import html as _html
        inc = self.incident_engine.acknowledge(incident_id, actor)
        if inc is None:
            return f"Unknown incident <code>{_html.escape(incident_id)}</code>."
        self.mirror_to_hub(f"{incident_id}-ACK", KIND_INCIDENT, inc.category, {"text": f"acknowledged by {actor}", "created_at": self.clock()},
                           "", label="Acknowledged", incident_id=incident_id, event_type="INCIDENT_ACKED", extra_detail={"actor": actor})
        self.on_event(f"incident {incident_id} acknowledged by {actor}")
        return (f"Acknowledged <code>{_html.escape(incident_id)}</code> by {_html.escape(actor)}. "
                "Reminders paused; the fault stays open until Studio no longer shows it.")

    def command_snooze(self, incident_id: str, minutes: int, actor: str) -> str:
        import html as _html
        if self.incident_engine.get(incident_id) is None:
            return f"Unknown incident <code>{_html.escape(incident_id)}</code>."
        until = self.incident_engine.snooze("incident", incident_id, minutes * 60, actor, "telegram")
        self.mirror_to_hub(f"{incident_id}-SNZ{int(self.clock())}", KIND_INCIDENT, "", {"text": f"snoozed {minutes} min by {actor}",
                           "created_at": self.clock()}, "", label="Snoozed", incident_id=incident_id, event_type="INCIDENT_SNOOZED",
                           extra_detail={"actor": actor, "until_utc": _utc(until)})
        self.on_event(f"incident {incident_id} snoozed {minutes} min by {actor}")
        return f"Snoozed <code>{_html.escape(incident_id)}</code> for {minutes} min (until {local_ts(until)})."

    def command_report(self) -> str:
        import html as _html
        a = self.activity
        summ = self.incident_engine.session_summary(self.device_id, self.sessions.session_id) if self.sessions.session_id else {"incidents": 0, "by_category": {}}
        lines = [f"<b>{_html.escape(self.cfg.notification_label)} \u2014 session report</b>",
                 f"Studio session: {a.session_id or 'none'} ({a.app_state.replace('_', ' ')})",
                 f"Broadcast: {a.live_state.replace('_', ' ')}" + (f", episode {a.broadcast_episode}" if a.broadcast_episode else ""),
                 f"Offline accumulated: {format_duration(a.offline_seconds)} \u00b7 reminders sent: {a.reminders_sent}",
                 f"Incidents this session: {summ['incidents']}"]
        for cat, b in summ["by_category"].items():
            lines.append(f"  \u2013 {cat}: {b['count']} ({b['open']} open, {b['resolved']} resolved, {format_duration(b['total_seconds'])} total)")
        if self.detectors is not None and a.stream:
            lines.append("Stream health: " + ", ".join(f"{k.lower()}={v.get('state')}" for k, v in a.stream.items()))
        open_incs = self.incident_engine.list(self.device_id, "OPEN", 3)
        query = "; ".join(i.summary[:120] for i in open_incs) or (self.last_report or {}).get("text_plain", "")[:200]
        similar = self.similar_from_memory(query)
        if similar:
            lines += ["", similar]
        return "\n".join(lines)

    def command_backend(self):
        mon = self

        class _Backend:
            def status(self): return mon.command_status()
            def screenshot(self): return mon.command_screenshot()
            def sessions(self, limit): return mon.command_sessions(limit)
            def ack(self, incident_id, actor): return mon.command_ack(incident_id, actor)
            def snooze(self, incident_id, minutes, actor): return mon.command_snooze(incident_id, minutes, actor)
            def report(self): return mon.command_report()
        return _Backend()

    def _check_schedule(self, confirmed: LiveState) -> None:
        sched = self.cfg.schedule
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.fromtimestamp(self.clock(), tz=_tz.utc)
        self.reminders.enabled = self.cfg.activity.reminders_enabled and sched.reminders_allowed(now)
        if not sched.enabled or self.sessions.state.session is None:
            return
        win = sched.window_at(now)
        key = win[0].isoformat() if win else None
        if key is None:
            self._missed_start_window = None
            return
        if sched.missed_start(now, confirmed.value) and self._missed_start_window != key:
            self._missed_start_window = key
            text = (f"\u23F0 <b>{self.cfg.notification_label} \u2014 SCHEDULED START MISSED</b>\n"
                    f"Scheduled window started {win[0].astimezone().strftime('%H:%M')} ({sched.describe()}); Studio is confirmed not live "
                    f"{sched.grace_minutes} minutes after the start.")
            self.dispatch(_event_id("SCH", self.clock()), KIND_ACTIVITY, CAT_REMINDERS,
                          {"text": text, "caption": text, "created_at": self.clock()}, "", label="Scheduled start missed")
            self.on_event("scheduled start missed (confirmed NOT_LIVE inside the scheduled window)")

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

    def _save_evidence(self, event_id: str, image) -> str:
        """Persist an already-redacted frame as evidence for an activity event."""
        if image is None or not self.cfg.privacy.send_screenshots:
            return ""
        dest = self.cfg.activity_screenshots_dir / f"{event_id}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.png")
        image.save(tmp, format="PNG")
        tmp.replace(dest)
        return str(dest)

    def _handle_session_event(self, ev: SessionEvent) -> None:
        act = self.cfg.activity
        eid = _event_id("EVT", ev.ts)
        if ev.type == EVT_OPENED:
            self._session_started_utc = ev.ts_utc
            self._stream_episode_counts = {}
            summary, category = "Studio opened", CAT_STUDIO_OPENED
            if not act.notify_opened:
                self.queue.record_event(eid, ev.type, ev.ts_utc, {"summary": summary, "notified": False}, ev.session_id)
                self.on_event(f"{summary} (session {ev.session_id}); notification disabled")
                return
            frame = self.frame_cache.fresh(act.fresh_screenshot_max_age_seconds) if ev.screenshot_available else None
            shot = self._activity_shot(eid, frame)
            payload = format_studio_opened(self.cfg.machine_label, ev.ts, bool(shot), act.open_screenshot_timeout_seconds,
                                           label=self.cfg.notification_label)
        elif ev.type == EVT_ALREADY_RUNNING:
            self._session_started_utc = ev.ts_utc
            summary, category = "Studio already running at monitor start", CAT_STUDIO_OPENED
            frame = self.frame_cache.fresh(act.fresh_screenshot_max_age_seconds) if ev.screenshot_available else None
            shot = self._activity_shot(eid, frame)
            payload = format_studio_already_running(self.cfg.machine_label, ev.ts, bool(shot),
                                                    label=self.cfg.notification_label)
        elif ev.type == EVT_CLOSED:
            summary, category = f"Studio closed ({ev.note})", CAT_STUDIO_CLOSED
            self._emit_report("session_report", "", ev.ts, session_id=ev.session_id)
            if not act.notify_closed:
                self.queue.record_event(eid, ev.type, ev.ts_utc, {"summary": summary, "notified": False}, ev.session_id)
                self.on_event(f"{summary}; notification disabled")
                return
            frame = self.frame_cache.latest()   # last frame *before* closure, whatever its age
            shot = self._activity_shot(eid, frame)
            payload = format_studio_closed(self.cfg.machine_label, ev.ts, frame.captured_at if shot else None,
                                           label=self.cfg.notification_label)
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
        self._last_live_text = text
        return self.live_rules.classify(text)

    def _update_broadcast_and_reminders(self, classification: Optional[Classification],
                                        evidence_cap: Optional[Capture]) -> None:
        cs = self.broadcast.observe(classification)
        self._check_schedule(cs.state)
        for old, new, why in self.broadcast.drain_transitions():
            self.last_transition = f"{old.value} -> {new.value} at {datetime.fromtimestamp(self.clock()):%H:%M:%S}"
            self.on_event(f"broadcast state {old.value} -> {new.value} ({why})")
        if classification is None:
            self.episodes.note_gap()
        bev = self.episodes.observe(cs.state, cs.fresh)
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
        if bev is not None:
            self._handle_broadcast_event(bev, evidence_cap)
        if out.due is not None:
            self._send_reminder(out.due)

    # ---- broadcast-start alert + account discovery ----------------------
    def current_account_handle(self) -> str:
        """Verified @handle for the *current* broadcast episode, else ''."""
        a = self.account
        if a.status == SUCCEEDED and a.username and a.episode_id and a.episode_id == self.episodes.state.episode_id:
            return a.handle
        return ""

    def account_snapshot(self) -> dict:
        a = self.account
        current = bool(a.episode_id) and a.episode_id == self.episodes.state.episode_id \
            and self.sessions.state.session is not None and (not a.session_id or a.session_id == self.sessions.session_id)
        return {"status": a.status, "username": a.username, "display_name": a.display_name, "source": a.source,
                "observed_utc": a.observed_utc, "error": a.error, "episode_id": a.episode_id, "attempts": a.attempts,
                "is_current": current, "in_progress": self._lookup is not None}

    def _handle_broadcast_event(self, bev, evidence_cap: Optional[Capture]) -> None:
        now = self.clock()
        if bev.kind == "ended":
            eid = _event_id("EVT", now)
            self.queue.record_event(eid, EVT_BROADCAST_ENDED, _utc(now),
                                    {"summary": "broadcast ended (confirmed NOT_LIVE)"}, episode_id=bev.episode_id)
            self.on_event(f"broadcast episode {bev.episode_id} ended (confirmed NOT_LIVE)")
            self._emit_report("broadcast_report", bev.episode_id, now)
            return
        eid = _event_id("BCS", now)
        # preserve the exact frame that supported the LIVE confirmation *before* anything else happens
        shot = self._save_evidence(eid, evidence_cap.image if evidence_cap is not None else None)
        pend = {"eid": eid, "bev": bev, "shot": shot, "ts": now}
        act = self.cfg.account
        if self.account.episode_id != bev.episode_id:
            # a new broadcast: previous identity is history, never reused as verified
            self.account = AccountIdentity(episode_id=bev.episode_id, session_id=self.sessions.session_id)
            self.identity_store.save(self.account)
        a = self.account
        if not act.detect_on_broadcast:
            a.status = DISABLED_STATUS
            self.identity_store.save(a)
            self._dispatch_broadcast(pend)
            return
        if a.status in (SUCCEEDED, FAILED, DISABLED_STATUS):
            # already looked up for this episode (e.g. monitor restart while live): never open the menu again
            self._dispatch_broadcast(pend)
            return
        if a.status == IN_PROGRESS:
            a.status, a.error = FAILED, "lookup interrupted by a monitor restart"
            self.identity_store.save(a)
            self._dispatch_broadcast(pend)
            return
        if self.interactor is None:
            a.status, a.error = FAILED, "no interaction backend available"
            self.identity_store.save(a)
            self._dispatch_broadcast(pend)
            return
        a.status, a.attempts = IN_PROGRESS, a.attempts + 1
        self.identity_store.save(a)
        self._pending_broadcast = pend
        self.broadcast.paused = True
        ctx = LookupContext(
            system=self.system, interactor=self.interactor, identity=self.tracker.identity,
            ocr=self.detector.ocr_text, fresh_frame=self._fresh_image, profile_region=self.cfg.profile_region,
            offset_right=act.profile_offset_right, offset_top=act.profile_offset_top, idle_required=act.idle_seconds,
            timeout=act.timeout_seconds, blocked=lambda: self._blocking_detection, allow_physical=act.allow_physical_click,
            clock=self.clock, mono=self.mono, sleep=self._lookup_sleep, log=self.on_event,
        )
        self._lookup = AccountLookupJob(ctx, bev.episode_id, self.mono())
        self.on_event(f"account lookup started for broadcast {bev.episode_id} (opens the profile menu once, "
                      f"{act.timeout_seconds:g} s budget); broadcast alert waits for the result")
        if self._inline_lookup:
            self._lookup._run()
        else:
            self._lookup.start()

    def _fresh_image(self):
        f = self.frames.frame()
        return f.image if f is not None else None

    def _poll_lookup(self) -> None:
        job = self._lookup
        if job is None:
            return
        studio_gone = self.sessions.state.session is None
        if not (job.done or job.expired(self.mono()) or studio_gone):
            return
        res = job.result
        if res is None:
            res = LookupResult(status=FAILED, error=("target exited during lookup" if studio_gone else
                                                     f"lookup timed out after {job.ctx.timeout:g} s"))
        a = self.account
        a.status = SUCCEEDED if res.status == SUCCEEDED else FAILED
        a.username, a.display_name, a.source, a.error = res.username, res.display_name, res.source, res.error
        a.attempts = max(a.attempts, res.attempts)
        a.observed_utc = _utc(res.observed_at or self.clock())
        self.identity_store.save(a)
        self.broadcast.paused = False
        self._lookup = None
        if res.steps:
            self.on_event("account lookup steps: " + "; ".join(res.steps))
        if a.status == SUCCEEDED:
            self.on_event(f"account detected: @{a.username} (source {a.source})")
        else:
            self.on_event(f"account lookup failed: {a.error or 'unknown reason'}")
        if not res.closed_menu:
            self.on_event("WARNING: the Studio profile menu may still be open")
        pend, self._pending_broadcast = self._pending_broadcast, None
        if pend is not None:
            self._dispatch_broadcast(pend)

    def _dispatch_broadcast(self, pend: dict) -> None:
        bev, eid, shot, now = pend["bev"], pend["eid"], pend["shot"], pend["ts"]
        a = self.account
        account_line = a.account_line()
        note = ""
        if a.status == FAILED and a.error:
            note = f"Account lookup: {a.error}"
        if bev.kind == "already_live":
            payload = format_already_live(self.cfg.machine_label, now, self.cfg.account_label, bool(shot),
                                          self.live_rules.verified, label=self.cfg.notification_label,
                                          account_line=account_line, account_note=note)
            summary, etype = "Studio is already LIVE (monitoring started)", EVT_ALREADY_LIVE
        else:
            payload = format_broadcast_started(self.cfg.machine_label, now, self.cfg.account_label,
                                               bev.kind == "started_after_gap", bev.gap_seconds, bool(shot),
                                               self.live_rules.verified, label=self.cfg.notification_label,
                                               account_line=account_line, account_note=note)
            summary = "Studio has gone LIVE" + (" (observed after a gap)" if bev.kind == "started_after_gap" else "")
            etype = EVT_BROADCAST_STARTED
        n = self.dispatch(eid, KIND_ACTIVITY, CAT_BROADCAST, payload, shot, label=summary)
        self.queue.record_event(eid, etype, _utc(now), {"summary": summary, "screenshot": bool(shot), "bots": n,
                                                        "kind": bev.kind, "account": a.handle or account_line,
                                                        "account_status": a.status, "account_source": a.source},
                                episode_id=bev.episode_id, screenshot_path=shot)
        self.on_event(f"{summary} -> {eid} queued for {n} bot(s){'' if shot else ' (text only)'}; TikTok account: {account_line}")

    def request_account_lookup(self) -> bool:
        """Explicit on-demand lookup (GUI/CLI button). Runs outside a broadcast
        event; results are stored but no broadcast alert is sent."""
        if self._lookup is not None or self.interactor is None:
            return False
        act = self.cfg.account
        a = self.account
        a.status, a.attempts, a.error = IN_PROGRESS, a.attempts + 1, ""
        a.episode_id = self.episodes.state.episode_id or a.episode_id
        a.session_id = self.sessions.session_id
        self.identity_store.save(a)
        self.broadcast.paused = True
        ctx = LookupContext(
            system=self.system, interactor=self.interactor, identity=self.tracker.identity,
            ocr=self.detector.ocr_text, fresh_frame=self._fresh_image, profile_region=self.cfg.profile_region,
            offset_right=act.profile_offset_right, offset_top=act.profile_offset_top, idle_required=act.idle_seconds,
            timeout=act.timeout_seconds, blocked=lambda: self._blocking_detection, allow_physical=act.allow_physical_click,
            clock=self.clock, mono=self.mono, sleep=self._lookup_sleep, log=self.on_event,
        )
        self._lookup = AccountLookupJob(ctx, a.episode_id, self.mono())
        self.on_event("manual account lookup started (opens the Studio profile menu once)")
        if self._inline_lookup:
            self._lookup._run()
        else:
            self._lookup.start()
        return True

    def _send_reminder(self, due: ReminderDue) -> None:
        act = self.cfg.activity
        now = self.clock()
        eid = _event_id("REM", now)
        frame = self.frame_cache.fresh(act.fresh_screenshot_max_age_seconds)
        shot = self._activity_shot(eid, frame)
        payload = format_not_live_reminder(
            self.cfg.machine_label, now, act.offline_threshold_minutes, due.accumulated_seconds, due.episode_id,
            due.sequence, act.repeat_max_count, bool(shot), self.live_rules.verified,
            label=self.cfg.notification_label,
        )
        details = {"summary": f"not-live reminder {due.sequence} after {int(due.accumulated_seconds)} s confirmed offline",
                   "sequence": due.sequence, "offline_seconds": due.accumulated_seconds, "screenshot": bool(shot)}
        n = self.reminders.enqueue_reminder(due, payload, shot, eid, details,
                                            [] if self.cfg.device.mode == "managed" else self.registry.targets(CAT_REMINDERS),
                                            CAT_REMINDERS, owner_label=self.cfg.notification_label)
        self.mirror_to_hub(eid, KIND_REMINDER, CAT_REMINDERS, payload, shot, label=f"Not-live reminder {due.sequence}",
                           event_type="NOT_LIVE_REMINDER")
        self.on_event(f"TIME TO GO LIVE reminder {due.sequence} -> {eid} queued for {n} bot(s)")
        self._kick()

    # ---- health --------------------------------------------------------
    def _update_health(self, state) -> None:
        cs = self.frames.status()
        h = self.health
        h.session = self.sessions.state.app_state
        h.broadcast = self.broadcast.state.state.value
        h.capture_backend = cs.backend
        h.last_valid_frame_at = cs.last_valid_at
        studio_running = self.sessions.state.session is not None
        if not studio_running:
            h.capture, h.capture_reason = "NONE", "Studio is not running"
        elif state.status == Status.LOST:
            h.capture, h.capture_reason = "DEGRADED", "Target window closed or not found"
        elif cs.health == "OK":
            # a stale frame (no fresh one within max age) is degraded even if the worker is alive
            age = self.mono() - cs.last_valid_mono if cs.last_valid_mono else None
            if age is not None and age > self.cfg.capture.max_frame_age_seconds:
                h.capture, h.capture_reason = "DEGRADED", "No fresh frame from the window"
            else:
                h.capture, h.capture_reason = "OK", (cs.reason if cs.code == "fallback" else "")
        elif cs.health == "DEGRADED":
            h.capture, h.capture_reason = "DEGRADED", cs.reason
        else:
            h.capture, h.capture_reason = ("DEGRADED", "Capture not started") if studio_running else ("NONE", "")
        d = self.queue.delivery_status()
        last = d.get("last")
        h.delivery = "FAILING" if last and last.get("status") in ("failed", "dead") else "OK"
        h.delivery_reason = (last.get("error") or "") if h.delivery == "FAILING" else ""
        degraded = studio_running and (h.capture == "DEGRADED" or h.ocr == "FAILING")
        alert = self.health_policy.update(degraded, h.degraded_reason)
        if alert is not None and self.registry.targets(CAT_HEALTH):
            payload = format_health_alert(alert.kind, alert.reason, self.cfg.machine_label, self.clock(),
                                          alert.since, alert.duration, label=self.cfg.notification_label)
            self.dispatch(_event_id("HLT", self.clock()), KIND_STATUS, CAT_HEALTH, payload, "",
                          label=f"Monitor health {alert.kind}")
            self.on_event(f"health alert queued: {alert.kind} ({alert.reason})")

    def _emit_activity(self) -> None:
        st = self.sessions.state
        bs = self.broadcast.state
        rs = self.reminders.state
        lc = self.broadcast.last_classification
        self.activity = ActivitySnapshot(
            app_state=st.app_state, session_id=self.sessions.session_id,
            live_state=bs.state.value, live_evidence=bs.evidence, live_rules_verified=self.live_rules.verified,
            last_confirmed_utc=bs.last_confirmed_utc, last_observation=lc.summary() if lc else "",
            last_transition=self.last_transition, broadcast_episode=self.episodes.state.episode_id,
            offline_seconds=rs.accumulated_seconds, remaining_seconds=self.reminders.remaining_seconds(),
            accumulating=self.reminders.accumulating, episode_id=rs.episode_id, reminders_sent=rs.reminders_sent,
            last_event=st.last_event, delivery=self.queue.delivery_status(), health=HealthSnapshot(**self.health.__dict__),
            account=self.account_snapshot(),
            capture=self.frames.status(), target_title=self.tracker.identity.title, target_hwnd=self.tracker.identity.hwnd,
            stream=dict(self.activity.stream), stream_end_hint=self._stream_end_hint,
            hub=self.hub_sync.status.to_dict() if self.hub_sync is not None else {},
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
        if self.hub_sync is not None:
            self.hub_sync.start()
        if self.command_poller is not None:
            self.command_poller.start()
        if self._watchdog_thread is None:
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="studio-monitor-watchdog", daemon=True)
            self._watchdog_thread.start()
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
            if self._owns_frames:
                self.frames.stop()
            if self.hub_sync is not None:
                self.hub_sync.stop()
            if self.command_poller is not None:
                self.command_poller.stop()
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
