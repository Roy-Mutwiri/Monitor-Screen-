"""Wiring helpers shared by the CLI and the GUI."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from .config import AppConfig, default_config_path, rules_path
from .detection.rules import RuleSet, load_rules
from .monitor import Monitor, ensure_dirs
from .ocr import create_backend
from .queue import DeliveryQueue
from .telegram import TelegramClient, make_sender


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


def load_config(path: Optional[Path] = None) -> tuple[AppConfig, Path]:
    path = path or default_config_path()
    cfg = AppConfig.load(path)
    cfg.apply_env_overrides()
    return cfg, path


def load_ruleset(cfg: AppConfig) -> RuleSet:
    return load_rules(rules_path(cfg))


def build_monitor(cfg: AppConfig, **callbacks) -> Monitor:
    from .win32.capture import Win32Capturer
    from .win32.windows import Win32WindowSystem

    ensure_dirs(cfg)
    system = Win32WindowSystem()
    capturer = Win32Capturer()
    ocr = create_backend(cfg.detection.ocr_backend, cfg.detection.ocr_language, cfg.detection.ocr_upscale)
    rules = load_ruleset(cfg)
    queue = DeliveryQueue(cfg.db_path, cfg.telegram.max_attempts,
                          cfg.telegram.backoff_base_seconds, cfg.telegram.backoff_max_seconds)
    sender = make_sender(TelegramClient(cfg.telegram))
    return Monitor(cfg, system, capturer, ocr, rules, queue, sender, **callbacks)
