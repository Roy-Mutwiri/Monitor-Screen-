"""Wiring helpers shared by the CLI and the GUI: config, logging, bot registry,
migrations and the monitor."""
from __future__ import annotations

import os

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from .bots import CAT_BROADCAST, CAT_HEALTH, BotRegistry, BotTarget
from .broadcast import LiveRules
from .config import AppConfig, TelegramConfig, default_config_path, live_rules_path, rules_path
from .credentials import CredentialStore, default_store
from .detection.rules import RuleSet, load_rules
from .monitor import Monitor, ensure_dirs
from .ocr import create_backend
from .queue import DeliveryQueue
from .telegram import ClientFactory, TokenRedactingFilter, sanitize

log = logging.getLogger(__name__)


def setup_logging(cfg: AppConfig, level: int = logging.INFO) -> None:
    ensure_dirs(cfg)
    log_path = cfg.data_path / "monitor.log"
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    if not any(isinstance(f, TokenRedactingFilter) for f in root.filters):
        root.addFilter(TokenRedactingFilter())
    for h in root.handlers:
        if not any(isinstance(f, TokenRedactingFilter) for f in h.filters):
            h.addFilter(TokenRedactingFilter())


def load_config(path: Optional[Path] = None) -> tuple[AppConfig, Path]:
    path = path or default_config_path()
    cfg = AppConfig.load(path)
    cfg.apply_env_overrides()
    return cfg, path


def load_ruleset(cfg: AppConfig) -> RuleSet:
    return load_rules(rules_path(cfg))


def load_live_rules(cfg: AppConfig) -> LiveRules:
    return LiveRules.load(live_rules_path(cfg))


def open_queue(cfg: AppConfig) -> DeliveryQueue:
    ensure_dirs(cfg)
    return DeliveryQueue(cfg.db_path, cfg.telegram.max_attempts, cfg.telegram.backoff_base_seconds,
                         cfg.telegram.backoff_max_seconds, sanitizer=sanitize)


def make_registry(cfg: AppConfig, cfg_path: Path, queue: Optional[DeliveryQueue] = None,
                  store: Optional[CredentialStore] = None) -> BotRegistry:
    store = store or default_store()
    return BotRegistry(cfg, store, save=lambda: cfg.save(cfg_path), queue=queue)


def run_migrations(cfg: AppConfig, cfg_path: Path, registry: BotRegistry, queue: DeliveryQueue) -> list[str]:
    """Versioned, restart-safe migrations from the single-bot layout.

    1. A legacy token/chat in settings (or environment) becomes "Default Bot"
       in the credential store; the token is removed from settings.
    2. Rows of the old ``alerts`` outbox become events + deliveries for that
       bot: pending ones exactly once, delivered ones as history only.
    Both steps are idempotent: step 1 is skipped when the token's fingerprint
    is already registered, step 2 marks migrated rows.
    """
    notes: list[str] = []
    tg = cfg.telegram
    if tg.bot_token and tg.chat_id:
        try:
            bot = registry.migrate_single_bot(tg.bot_token, tg.chat_id)
        except Exception as exc:
            notes.append(f"legacy Telegram settings could not be moved to the credential store: {sanitize(str(exc))}; "
                         "the token stays in memory for this run only")
            bot = None
        if bot is not None:
            if not tg.notify_status_changes and CAT_HEALTH in bot.subscriptions:
                bot.subscriptions.remove(CAT_HEALTH)
            notes.append(f"migrated the single Telegram bot into '{bot.name}' (token now in the credential store)")
        if bot is not None or registry.count:
            tg.bot_token = ""
            tg.chat_id = ""
            tg.legacy_migrated = True
            cfg.save(cfg_path)
    if cfg.config_version < 4:
        added = 0
        for b in registry.bots:
            if b.enabled and CAT_BROADCAST not in b.subscriptions:
                b.subscriptions.append(CAT_BROADCAST)
                added += 1
        cfg.config_version = 4
        cfg.save(cfg_path)
        if added:
            notes.append(f"subscribed {added} enabled bot(s) to the new 'Broadcast started / already live' category")
    if cfg.config_version < 5:
        from .bots import CAT_STREAM
        added = 0
        for b in registry.bots:
            if b.enabled and CAT_STREAM not in b.subscriptions:
                b.subscriptions.append(CAT_STREAM)
                added += 1
        cfg.config_version = 5
        cfg.save(cfg_path)
        if added:
            notes.append(f"subscribed {added} enabled bot(s) to the new 'Stream health' category")
    if cfg.config_version < 6:
        from .bots import CAT_POPUPS
        added = 0
        for b in registry.bots:
            if b.enabled and CAT_POPUPS not in b.subscriptions:
                b.subscriptions.append(CAT_POPUPS)
                added += 1
        if cfg.detection.poll_interval_seconds == 2.0:
            cfg.detection.poll_interval_seconds = 1.0           # faster popup detection (measured; see latency diagnostics)
        cfg.config_version = 6
        cfg.save(cfg_path)
        if added:
            notes.append(f"subscribed {added} enabled bot(s) to the new 'Unrecognised popups' category")
    if queue.legacy_pending_count() or not queue.get_state("legacy_alerts_migrated"):
        target = None
        default = registry.by_name("Default Bot") or (registry.bots[0] if registry.bots else None)
        if default is not None:
            target = BotTarget(default.bot_id, default.name, default.chat_id, default.thread_id)
        if target is not None or not queue.legacy_pending_count():
            done = queue.migrate_legacy_alerts(target)
            queue.set_state("legacy_alerts_migrated", True)
            if done["pending"] or done["history"]:
                notes.append(f"migrated legacy outbox: {done['pending']} pending, {done['history']} history row(s)")
    return notes


def make_detector_suite(cfg: AppConfig, clock=None, mono=None):
    """Detector suite for the real app: YuNet face backend when the bundled model verifies, else presenter
    conditions are DISABLED (never silently approximated)."""
    import time
    from .config import connection_rules_path
    from .detectors.presenter import YuNetDetector
    from .detectors.suite import DetectorSuite, DetectorsConfig
    from .detectors.text_rules import ConnectionRules
    d = cfg.detectors
    if not d.enabled:
        return None
    dc = DetectorsConfig(**{k: getattr(d, k) for k in DetectorsConfig.__dataclass_fields__ if hasattr(d, k)})
    try:
        rules = ConnectionRules.load(connection_rules_path(cfg))
    except Exception as exc:
        log.warning("connection rules unavailable (%s); text detectors disabled", exc)
        rules = ConnectionRules({})
    backend = None
    if d.presenter_enabled:
        try:
            backend = YuNetDetector()
        except Exception as exc:
            log.warning("face detector unavailable: %s; presenter conditions DISABLED", exc)
    return DetectorSuite(dc, rules, backend, clock or time.time, mono or time.monotonic)


def make_hub_sync(cfg: AppConfig, store: Optional[CredentialStore] = None, on_event=None, clock=None, mono=None):
    """HubSync for an enrolled agent, else None. The secret comes from the credential store only."""
    import time
    from .hub_client import HubClient, load_secret
    from .hub_outbox import HubOutbox
    from .hub_sync import HubSync
    if not cfg.hub.url or not cfg.hub.enrolled:
        return None
    cfg.ensure_device_id()
    secret = load_secret(store or default_store(), cfg.device.device_id)
    if not secret:
        log.warning("hub enrollment is recorded but no agent credential is stored; run `studio-monitor hub enroll` again")
        return None
    client = HubClient(cfg.hub.url, cfg.device.device_id, secret, verify=cfg.hub.verify_tls)
    outbox = HubOutbox(cfg.db_path, clock or time.time)
    return HubSync(client, outbox, lambda: {}, heartbeat_seconds=cfg.hub.heartbeat_seconds,
                   upload_evidence=cfg.hub.upload_evidence, clock=clock or time.time, mono=mono or time.monotonic,
                   on_event=on_event)


def enroll_agent(cfg: AppConfig, cfg_path: Path, url: str, code: str, mode: Optional[str] = None,
                 store: Optional[CredentialStore] = None, transport=None) -> dict:
    """Exchange a single-use pairing code for an agent credential, store it in the credential store and
    record the enrollment in settings. Never logs the secret."""
    import socket
    from .hub_client import HubClient, save_secret
    from .contracts.events import utc_now_iso
    store = store or default_store()
    cfg.ensure_device_id()
    if mode:
        if mode not in ("standalone", "managed"):
            raise ValueError("mode must be standalone or managed")
        cfg.device.mode = mode
    client = HubClient(url, transport=transport, verify=cfg.hub.verify_tls)
    try:
        res = client.enroll(code, cfg.device.device_id, cfg.device.device_name or cfg.machine_label, socket.gethostname(),
                            cfg.device.mode, cfg.notification_label, cfg.device.expected_account)
    finally:
        client.close()
    save_secret(store, cfg.device.device_id, res.secret)
    cfg.hub.url, cfg.hub.enrolled, cfg.hub.workspace_id = url.rstrip("/"), True, res.workspace_id
    cfg.hub.enrolled_utc = utc_now_iso()
    cfg.hub.heartbeat_seconds = float(res.heartbeat_interval or cfg.hub.heartbeat_seconds)
    cfg.save(cfg_path)
    return {"device_id": res.device_id, "workspace_id": res.workspace_id, "heartbeat_interval": res.heartbeat_interval,
            "unreachable_after": res.unreachable_after, "mode": cfg.device.mode}


def unenroll_agent(cfg: AppConfig, cfg_path: Path, store: Optional[CredentialStore] = None) -> None:
    from .hub_client import clear_secret
    store = store or default_store()
    try:
        clear_secret(store, cfg.device.device_id)
    except Exception as exc:  # pragma: no cover
        log.warning("could not remove the agent credential: %s", exc)
    cfg.hub.enrolled, cfg.hub.workspace_id, cfg.hub.enrolled_utc = False, "", ""
    cfg.save(cfg_path)


def make_command_poller(cfg: AppConfig, monitor, registry: BotRegistry, factory, on_event=None):
    """UpdatePoller for the command bot (standalone mode only; the hub answers commands for managed devices)."""
    from .commands import CommandRouter, UpdatePoller
    if not cfg.commands.enabled or cfg.device.mode == "managed" or factory is None:
        return None
    bot = next((b for b in cfg.bots if b.enabled and (not cfg.commands.bot_id or b.bot_id == cfg.commands.bot_id)), None)
    if bot is None:
        return None
    token = factory.token(bot.bot_id)
    if not token:
        return None
    from .telegram import TelegramClient
    tg_cfg = TelegramConfig(**{**cfg.telegram.__dict__, "timeout_seconds": max(cfg.telegram.timeout_seconds, cfg.commands.long_poll_seconds + 15)})
    client = TelegramClient(tg_cfg, token, bot.chat_id, bot.thread_id, transport=factory.transport)
    router = CommandRouter(monitor.command_backend(), {bot.chat_id}, on_audit=on_event or monitor.on_event,
                           rate_per_minute=cfg.commands.rate_per_minute)
    cfg.ensure_device_id()
    return UpdatePoller(client, router, monitor.queue.get_state, monitor.queue.set_state, bot.bot_id,
                        f"{cfg.device.device_id}:{os.getpid()}", on_event=on_event or monitor.on_event,
                        long_poll_seconds=cfg.commands.long_poll_seconds)


def make_email_backup(cfg: AppConfig, store: Optional[CredentialStore] = None):
    from .email_backup import EmailBackup, SmtpSettings
    s = cfg.smtp
    settings = SmtpSettings(s.enabled, s.host, s.port, s.username, s.from_addr, list(s.to_addrs), s.starttls, 20.0, s.min_severity)
    return EmailBackup(settings, store or default_store())


def make_memory_provider(cfg: AppConfig, store: Optional[CredentialStore] = None, client=None):
    """Supermemory provider for a standalone PC. Managed devices leave memory to the hub (the hub's key never
    reaches agents). Without a locally entered key the provider is None and the UI says so."""
    from .memory import load_api_key, make_provider
    if cfg.device.mode == "managed" or not cfg.memory.enabled:
        return None
    key = load_api_key(store or default_store())
    if not key:
        log.warning("memory enabled but no Supermemory key in the credential store; run `studio-monitor memory set-key`")
        return None
    cfg.ensure_device_id()
    ns = cfg.memory.namespace or f"studio-monitor-{cfg.device.device_id[:8]}"
    return make_provider(True, ns, key, client=client)


def make_pc_health(cfg: AppConfig, ps=None):
    from .pc_health import PcHealthConfig, PcHealthSampler
    p = cfg.pc_health
    if not p.enabled:
        return None
    pc = PcHealthConfig(p.enabled, p.interval_seconds, p.cpu_percent, p.memory_percent, p.disk_free_percent, p.battery_percent,
                        p.upload_kbps_min, p.sustain_seconds, p.recover_seconds)
    return PcHealthSampler(pc, cfg.data_dir, ps=ps)


def make_clips(cfg: AppConfig):
    from .clips import ClipBuffer, ClipsConfig
    c = cfg.clips
    if not c.enabled:
        return None
    return ClipBuffer(ClipsConfig(c.enabled, c.seconds_before, c.fps, c.max_width, c.send))


def doctor(cfg: AppConfig, cfg_path: Path) -> list[tuple[str, str, str]]:
    """Environment checks: (name, OK|WARN|FAIL, detail). Read-only."""
    out = []
    import shutil
    from .credentials import default_store
    try:
        store = default_store()
        probe = "doctor/probe"
        store.set(probe, "x"); ok = store.get(probe) == "x"; store.delete(probe)
        out.append(("credential store", "OK" if ok else "FAIL", "Windows Credential Manager read/write"))
    except Exception as exc:
        out.append(("credential store", "FAIL", str(exc)[:120]))
    try:
        from .ocr import available_backends
        b = available_backends()
        out.append(("OCR backends", "OK" if b else "FAIL", ", ".join(b) or "none available (install winocr)"))
    except Exception as exc:
        out.append(("OCR backends", "FAIL", str(exc)[:120]))
    try:
        import windows_capture  # noqa: F401
        out.append(("Windows Graphics Capture", "OK", "windows-capture importable"))
    except Exception as exc:
        out.append(("Windows Graphics Capture", "WARN", f"not importable ({str(exc)[:80]}); PrintWindow fallback is blank for Studio"))
    from .detectors.presenter import YuNetDetector, model_path
    out.append(("face model", "OK" if YuNetDetector.available() else "WARN", model_path()))
    usage = shutil.disk_usage(cfg.data_dir if os.path.isdir(cfg.data_dir) else os.path.dirname(cfg_path) or ".")
    free_pct = usage.free / usage.total * 100
    out.append(("disk space", "OK" if free_pct > 5 else "WARN", f"{free_pct:.0f}% free on the data drive"))
    out.append(("target", "OK" if cfg.target.is_set else "WARN", cfg.target.title or "no Studio window selected"))
    out.append(("bots", "OK" if any(b.enabled for b in cfg.bots) else "WARN", f"{sum(1 for b in cfg.bots if b.enabled)} enabled"))
    out.append(("live-state rules", "WARN", "seeded, unverified against real Studio screenshots (calibrate-live)"))
    try:
        from .startup import is_enabled
        out.append(("start at sign-in", "OK", "enabled" if is_enabled() else "disabled"))
    except Exception:
        pass
    if cfg.hub.url:
        out.append(("hub", "OK" if cfg.hub.enrolled else "WARN", f"{cfg.hub.url} enrolled={cfg.hub.enrolled} mode={cfg.device.mode}"))
    else:
        out.append(("hub", "OK", "not configured (standalone)"))
    return out


def make_layout_tracker(cfg: AppConfig, on_event=None, hwnd_provider=None):
    """LayoutTracker with its own OCR instance (word boxes), the UIA probe and optional external parser."""
    import time
    from .perception.layout import LayoutStore
    from .perception.tracker import LayoutTracker
    from .perception.uia import uia_elements
    if not cfg.perception.enabled:
        return None
    try:
        from .ocr import create_backend
        ocr = create_backend(cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
    except Exception as exc:
        log.warning("perception OCR unavailable: %s", exc)
        return None

    def probe():
        hwnd, origin = (hwnd_provider() if hwnd_provider else (cfg.target.hwnd, (0, 0)))
        return uia_elements(hwnd, origin) if hwnd else ([], "no window")

    extra = None
    p = cfg.perception
    if p.omniparser_enabled:
        from .perception.omniparser import OmniParserBackend
        if OmniParserBackend.available(p.omniparser_python, p.omniparser_model):
            extra = OmniParserBackend(p.omniparser_python, p.omniparser_model, p.omniparser_timeout_seconds)
        else:
            log.warning("OmniParser enabled but its environment/model path is not valid; ignored")
    ensure_dirs(cfg)
    return LayoutTracker(ocr, LayoutStore(cfg.data_path / "layout_profiles.json"), uia_probe=probe,
                         discovery_interval=p.discovery_interval_seconds, validate_interval=p.validate_interval_seconds,
                         on_event=on_event, extra_parser=extra)


def make_audio_worker(cfg: AppConfig, on_event=None):
    from .audio.levels import AudioAnalyzer, AudioConfig
    from .audio.resolver import AudioSourceResolver, pycaw_endpoints, pycaw_sessions
    from .audio.worker import AudioWorker, session_peak_reader
    from .audio import process_loopback as pl
    a = cfg.listening
    if not a.enabled or a.preference == "off":
        return None
    resolver = AudioSourceResolver(sessions=pycaw_sessions, endpoints=pycaw_endpoints, loopback_probe=lambda pid: pl.probe(pid, 1.0)[0])
    analyzer = AudioAnalyzer(AudioConfig(a.silence_seconds, a.silence_dbfs, 0.985, a.clipping_seconds, 5.0, 3.0, a.vad))
    transcriber = None
    if a.transcription_enabled:
        try:
            from .audio.transcribe import ChunkedTranscriber, FasterWhisperTranscriber
            transcriber = ChunkedTranscriber(FasterWhisperTranscriber(a.transcription_model))
        except Exception as exc:
            log.warning("transcription unavailable: %s", exc)
    return AudioWorker(resolver, analyzer, preference=a.preference,
                       loopback_factory=lambda pid, cb: pl.ProcessLoopbackCapture(pid, cb), meter_reader=session_peak_reader,
                       transcriber=transcriber, on_event=on_event)


def _window_origin(cfg: AppConfig) -> tuple[int, int]:
    try:
        from .win32.windows import Win32WindowSystem
        w = Win32WindowSystem().get_window(cfg.target.hwnd)
        return (w.rect.left, w.rect.top) if w else (0, 0)
    except Exception:
        return (0, 0)


def make_capture_service(cfg: AppConfig, system=None):
    from .win32.capture import CaptureService
    from .win32.windows import Win32WindowSystem
    c = cfg.capture
    return CaptureService(system or Win32WindowSystem(), interval=c.interval_seconds, max_age=c.max_frame_age_seconds,
                          refresh_interval=c.refresh_interval_seconds, allow_desktop_fallback=c.allow_desktop_fallback,
                          prefer=c.backend)


def build_monitor(cfg: AppConfig, cfg_path: Path, registry: Optional[BotRegistry] = None,
                  queue: Optional[DeliveryQueue] = None, **callbacks) -> Monitor:
    from .win32.capture import Win32Capturer
    from .win32.windows import Win32WindowSystem

    ensure_dirs(cfg)
    system = Win32WindowSystem()
    capturer = Win32Capturer(allow_screen_fallback=False, system=system)   # dialogs: PrintWindow only
    if callbacks.get("frame_service") is None:
        callbacks["frame_service"] = make_capture_service(cfg, system)
        callbacks["owns_frame_service"] = True
    ocr = create_backend(cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
    rules = load_ruleset(cfg)
    queue = queue or open_queue(cfg)
    registry = registry or make_registry(cfg, cfg_path, queue)
    if registry.queue is None:
        registry.queue = queue
    for note in run_migrations(cfg, cfg_path, registry, queue):
        log.info(note)
    factory = ClientFactory(cfg.telegram, registry.token_for)
    callbacks.setdefault("detector_suite", make_detector_suite(cfg))
    if "hub_sync" not in callbacks:
        callbacks["hub_sync"] = make_hub_sync(cfg, on_event=callbacks.get("on_event"))
    callbacks.setdefault("email_backup", make_email_backup(cfg))
    callbacks.setdefault("memory", make_memory_provider(cfg))
    callbacks.setdefault("pc_health", make_pc_health(cfg))
    callbacks.setdefault("clips", make_clips(cfg))
    if "layout_tracker" not in callbacks:
        callbacks["layout_tracker"] = make_layout_tracker(cfg, callbacks.get("on_event"),
                                                          hwnd_provider=lambda: (cfg.target.hwnd, _window_origin(cfg)))
    if "audio_worker" not in callbacks:
        callbacks["audio_worker"] = make_audio_worker(cfg, callbacks.get("on_event"))
    monitor = Monitor(cfg, system, capturer, ocr, rules, queue, registry, factory,
                      live_rules=load_live_rules(cfg), **callbacks)
    if monitor.command_poller is None:
        monitor.command_poller = make_command_poller(cfg, monitor, registry, factory, callbacks.get("on_event"))
    return monitor
