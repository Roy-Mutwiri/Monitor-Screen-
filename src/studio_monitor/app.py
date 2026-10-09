"""Wiring helpers shared by the CLI and the GUI: config, logging, bot registry,
migrations and the monitor."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from .bots import CAT_BROADCAST, CAT_HEALTH, BotRegistry, BotTarget
from .broadcast import LiveRules
from .config import AppConfig, default_config_path, live_rules_path, rules_path
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
    return Monitor(cfg, system, capturer, ocr, rules, queue, registry, factory,
                   live_rules=load_live_rules(cfg), **callbacks)
