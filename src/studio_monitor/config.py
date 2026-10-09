"""Configuration model with JSON load/save."""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .regions import Region


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "TikTokLiveStudioMonitor"


@dataclass
class TargetIdentity:
    """What we learned about the Studio window the user picked.

    ``exe_name`` and ``exe_path`` are discovered from the live process at
    selection time, never assumed.
    """
    hwnd: int = 0
    pid: int = 0
    exe_path: str = ""
    exe_name: str = ""
    class_name: str = ""
    title: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.exe_name and self.class_name)


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    timeout_seconds: float = 20.0
    max_attempts: int = 8
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 300.0
    notify_status_changes: bool = False  # also send LOST/DEGRADED/RUNNING transitions to Telegram


@dataclass
class PrivacyConfig:
    send_screenshots: bool = True
    screenshot_retention_days: int = 7
    store_detected_text: bool = True
    log_ocr_text: bool = False
    max_text_in_alert: int = 400


@dataclass
class DetectionConfig:
    poll_interval_seconds: float = 2.0
    confirm_polls: int = 2            # popup must be seen in N consecutive polls
    dedup_cooldown_seconds: float = 600.0
    resolve_after_seconds: float = 30.0
    rules_file: str = ""              # empty -> bundled rules/studio_rules.json
    ocr_backend: str = "auto"
    ocr_language: str = "en"
    ocr_upscale: float = 1.0
    include_dialogs: bool = True
    lost_window_grace_seconds: float = 15.0


@dataclass
class AppConfig:
    machine_label: str = field(default_factory=socket.gethostname)
    data_dir: str = field(default_factory=lambda: str(default_data_dir()))
    target: TargetIdentity = field(default_factory=TargetIdentity)
    regions: list[Region] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["regions"] = [r.to_dict() for r in self.regions]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        cfg = cls()
        cfg.machine_label = str(data.get("machine_label") or cfg.machine_label)
        cfg.data_dir = str(data.get("data_dir") or cfg.data_dir)
        cfg.target = TargetIdentity(**_known(TargetIdentity, data.get("target", {})))
        cfg.regions = [Region.from_dict(r) for r in data.get("regions", [])]
        cfg.telegram = TelegramConfig(**_known(TelegramConfig, data.get("telegram", {})))
        cfg.privacy = PrivacyConfig(**_known(PrivacyConfig, data.get("privacy", {})))
        cfg.detection = DetectionConfig(**_known(DetectionConfig, data.get("detection", {})))
        return cfg

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def screenshots_dir(self) -> Path:
        return self.data_path / "screenshots"

    @property
    def db_path(self) -> Path:
        return self.data_path / "monitor.sqlite3"

    def apply_env_overrides(self) -> None:
        """Secrets may be supplied via environment instead of the config file."""
        tok = os.environ.get("STUDIO_MONITOR_TELEGRAM_TOKEN")
        chat = os.environ.get("STUDIO_MONITOR_TELEGRAM_CHAT_ID")
        label = os.environ.get("STUDIO_MONITOR_MACHINE_LABEL")
        if tok:
            self.telegram.bot_token = tok
        if chat:
            self.telegram.chat_id = chat
        if label:
            self.machine_label = label


def _known(cls, data: dict) -> dict:
    names = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return {k: v for k, v in (data or {}).items() if k in names}


def default_config_path() -> Path:
    return default_data_dir() / "config.json"


def rules_path(cfg: Optional[AppConfig] = None) -> Path:
    if cfg and cfg.detection.rules_file:
        return Path(cfg.detection.rules_file)
    return bundled_rules_path()


def bundled_rules_path() -> Path:
    import sys
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(getattr(sys, "_MEIPASS", ".")) / "rules" / "studio_rules.json"
    return Path(__file__).resolve().parents[2] / "rules" / "studio_rules.json"
