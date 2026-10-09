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
    monitor = Monitor(cfg, system, capturer, ocr, rules, queue, registry, factory,
                      live_rules=load_live_rules(cfg), **callbacks)
    if monitor.command_poller is None:
        monitor.command_poller = make_command_poller(cfg, monitor, registry, factory, callbacks.get("on_event"))
    return monitor
