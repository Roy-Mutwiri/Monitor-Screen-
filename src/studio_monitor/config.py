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
    process_start: float = 0.0   # process creation time (epoch); with pid + exe it defeats pid reuse

    @property
    def is_set(self) -> bool:
        return bool(self.exe_name and self.class_name)


@dataclass
class TelegramConfig:
    # Legacy single-bot fields: only used to migrate into the bot registry
    # (or supplied via environment). Tokens are never kept here afterwards.
    bot_token: str = ""
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    timeout_seconds: float = 20.0
    max_attempts: int = 8
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 300.0
    notify_status_changes: bool = False  # also send LOST/DEGRADED/RUNNING transitions (health category)
    fingerprint_salt: str = ""           # per-install salt for non-reversible token fingerprints
    delivery_concurrency: int = 4        # bots served in parallel by the outbox worker
    delivery_max_age_hours: float = 48.0 # pending deliveries older than this are dead-lettered
    legacy_migrated: bool = False


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
class ActivityConfig:
    """Studio opened/closed notifications and the not-live reminder."""
    notify_opened: bool = True
    notify_closed: bool = True
    notify_already_running: bool = True
    open_screenshot_timeout_seconds: float = 30.0   # then send the OPENED notice as text
    close_debounce_seconds: float = 10.0            # process must be gone this long
    reminders_enabled: bool = True
    offline_threshold_minutes: float = 60.0
    repeat_enabled: bool = False
    repeat_interval_minutes: float = 60.0
    repeat_max_count: int = 3
    max_observation_gap_seconds: float = 30.0       # larger gaps never count as offline time
    confirm_observations: int = 3                   # consecutive observations to confirm a state
    fresh_screenshot_max_age_seconds: float = 15.0  # "fresh" frame for reminder/opened notices
    live_rules_file: str = ""                       # empty -> bundled rules/live_state_rules.json
    start_at_signin: bool = False


@dataclass
class CaptureConfig:
    backend: str = "auto"                     # auto | wgc | printwindow
    interval_seconds: float = 0.5
    max_frame_age_seconds: float = 30.0       # older frames are never used as current evidence
    refresh_interval_seconds: float = 15.0    # WGC session heartbeat restart when no new frame arrived
    allow_desktop_fallback: bool = True       # explicit, visibility-verified desktop crop as last resort


@dataclass
class HealthConfig:
    degrade_after_seconds: float = 15.0
    recover_after_seconds: float = 10.0


@dataclass
class AccountConfig:
    """Automatic TikTok @username discovery when a broadcast starts."""
    detect_on_broadcast: bool = True
    timeout_seconds: float = 10.0
    idle_seconds: float = 1.5                 # user inactivity required before any physical interaction
    allow_physical_click: bool = True         # UI Automation is tried first; Studio normally exposes no tree
    profile_offset_right: int = 190           # default avatar position (px from the right edge / top) when no
    profile_offset_top: int = 24              # 'profile' region has been calibrated on the preview


@dataclass
class AccountConfig:
    """Automatic TikTok @username discovery when a broadcast starts."""
    detect_on_broadcast: bool = True
    timeout_seconds: float = 10.0
    idle_seconds: float = 1.5                 # user inactivity required before any physical interaction
    allow_physical_click: bool = True         # UI Automation is tried first; Studio normally exposes no tree
    profile_offset_right: int = 190           # default avatar position (px from the right edge / top) when no
    profile_offset_top: int = 24              # 'profile' region has been calibrated on the preview


@dataclass
class AccountConfig:
    """Automatic TikTok @username discovery when a broadcast starts."""
    detect_on_broadcast: bool = True
    timeout_seconds: float = 10.0
    idle_seconds: float = 1.5                 # user inactivity required before any physical interaction
    allow_physical_click: bool = True         # UI Automation is tried first; Studio normally exposes no tree
    profile_offset_right: int = 190           # default avatar position (px from the right edge / top) when no
    profile_offset_top: int = 24              # 'profile' region has been calibrated on the preview


@dataclass
class UiConfig:
    theme: str = "bootstrap-dark"   # ttkbootstrap theme name (bootstrap-dark | bootstrap-light)


CONFIG_VERSION = 4


@dataclass
class AppConfig:
    machine_label: str = field(default_factory=socket.gethostname)
    data_dir: str = field(default_factory=lambda: str(default_data_dir()))
    target: TargetIdentity = field(default_factory=TargetIdentity)
    regions: list[Region] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    activity: ActivityConfig = field(default_factory=ActivityConfig)
    bots: list = field(default_factory=list)   # list[BotConfig]; tokens live in the credential store
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    account_label: str = ""                    # optional operator label shown in broadcast alerts
    owner_name: str = ""                       # "Whose PC?" -> notification label "<owner>'s Live"
    ui: UiConfig = field(default_factory=UiConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    config_version: int = CONFIG_VERSION

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["regions"] = [r.to_dict() for r in self.regions]
        d["bots"] = [b.to_dict() if hasattr(b, "to_dict") else b for b in self.bots]
        d["config_version"] = CONFIG_VERSION
        tg = d["telegram"]
        tg["bot_token"] = ""   # never written to disk; it lives in the credential store after migration
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        """Tolerant loader: unknown keys are ignored and missing sections get
        defaults, so configs written by older versions load unchanged."""
        cfg = cls()
        cfg.machine_label = str(data.get("machine_label") or cfg.machine_label)
        cfg.data_dir = str(data.get("data_dir") or cfg.data_dir)
        cfg.target = TargetIdentity(**_known(TargetIdentity, data.get("target", {})))
        cfg.regions = [Region.from_dict(r) for r in data.get("regions", [])]
        cfg.telegram = TelegramConfig(**_known(TelegramConfig, data.get("telegram", {})))
        cfg.privacy = PrivacyConfig(**_known(PrivacyConfig, data.get("privacy", {})))
        cfg.detection = DetectionConfig(**_known(DetectionConfig, data.get("detection", {})))
        cfg.activity = ActivityConfig(**_known(ActivityConfig, data.get("activity", {})))
        cfg.capture = CaptureConfig(**_known(CaptureConfig, data.get("capture", {})))
        cfg.health = HealthConfig(**_known(HealthConfig, data.get("health", {})))
        cfg.ui = UiConfig(**_known(UiConfig, data.get("ui", {})))
        cfg.account = AccountConfig(**_known(AccountConfig, data.get("account", {})))
        cfg.account = AccountConfig(**_known(AccountConfig, data.get("account", {})))
        cfg.account = AccountConfig(**_known(AccountConfig, data.get("account", {})))
        if cfg.ui.theme not in ("bootstrap-dark", "bootstrap-light"):
            cfg.ui.theme = "bootstrap-dark"
        cfg.account_label = str(data.get("account_label", "") or "")
        from .labels import validate_owner_name, OwnerNameError
        try:
            cfg.owner_name = validate_owner_name(str(data.get("owner_name", "") or ""))
        except OwnerNameError:
            cfg.owner_name = ""
        from .bots import BotConfig
        cfg.bots = [BotConfig.from_dict(b) for b in data.get("bots", []) if isinstance(b, dict)]
        cfg.config_version = int(data.get("config_version", 1))
        return cfg

    @property
    def notification_label(self) -> str:
        """Shared label for every notification: "<owner>'s Live" (machine label fallback)."""
        from .labels import notification_label
        return notification_label(self.owner_name, self.machine_label)

    @property
    def live_regions(self) -> list[Region]:
        return [r for r in self.regions if r.kind == "live"]

    @property
    def profile_region(self) -> Optional[Region]:
        return next((r for r in self.regions if r.kind == "profile"), None)

    @property
    def profile_region(self) -> Optional[Region]:
        return next((r for r in self.regions if r.kind == "profile"), None)

    @property
    def profile_region(self) -> Optional[Region]:
        return next((r for r in self.regions if r.kind == "profile"), None)

    @property
    def frame_cache_dir(self) -> Path:
        return self.data_path / "latest_frame"

    @property
    def activity_screenshots_dir(self) -> Path:
        return self.data_path / "activity_screenshots"

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


def bundled_rules_path(name: str = "studio_rules.json") -> Path:
    import sys
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(getattr(sys, "_MEIPASS", ".")) / "rules" / name
    return Path(__file__).resolve().parents[2] / "rules" / name


def live_rules_path(cfg: Optional[AppConfig] = None) -> Path:
    if cfg and cfg.activity.live_rules_file:
        return Path(cfg.activity.live_rules_file)
    return bundled_rules_path("live_state_rules.json")
