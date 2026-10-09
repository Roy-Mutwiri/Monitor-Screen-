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
    session_reports: bool = True        # broadcast / session reports when a broadcast or Studio session ends
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
class DetectorsSettings:
    """Stream-health detectors (presenter/face, preview, source, connection, audio)."""
    enabled: bool = True
    presenter_enabled: bool = True
    presenter_expected: bool = True
    face_absent_seconds: float = 30.0
    motion_low_seconds: float = 60.0
    motion_threshold: float = 0.035
    frozen_seconds: float = 20.0
    black_preview_seconds: float = 20.0
    black_luma: float = 12.0
    source_sustain_seconds: float = 10.0
    connection_sustain_seconds: float = 10.0
    connection_recover_seconds: float = 20.0
    recover_seconds: float = 15.0
    scene_change_grace_seconds: float = 10.0
    audio_enabled: bool = False
    audio_silence_seconds: float = 30.0
    audio_level_threshold: float = 0.05
    audio_profile: str = "mixed"
    rules_file: str = ""


def install_fingerprint(data_dir: str = "") -> str:
    """Per-installation fingerprint (machine GUID + Windows user + data dir). A copied install on another
    PC or user account yields a different value and therefore enrolls as a *new* device."""
    import hashlib
    import platform
    parts = [platform.node(), os.environ.get("USERNAME", ""), os.path.normcase(data_dir or "")]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            parts.append(str(winreg.QueryValueEx(k, "MachineGuid")[0]))
    except Exception:
        parts.append("no-machine-guid")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


@dataclass
class CommandsConfig:
    """Telegram commands and inline buttons (standalone mode; in managed mode the hub answers)."""
    enabled: bool = True
    bot_id: str = ""                    # which bot listens for commands; empty = first enabled bot
    buttons: bool = True                # inline Ack / Snooze / Screenshot buttons under incident alerts
    rate_per_minute: int = 12
    long_poll_seconds: int = 20


@dataclass
class EscalationConfig:
    """Extra destination once an incident has gone unacknowledged through N reminders."""
    enabled: bool = False
    after_reminders: int = 2
    bot_id: str = ""                    # bot used for the escalation message (empty = same bot as the alert)
    chat_id: str = ""                   # escalation chat (e.g. a second person / group)
    thread_id: Optional[int] = None


@dataclass
class SmtpConfig:
    """E-mail backup when Telegram delivery of an urgent event has definitively failed."""
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    starttls: bool = True
    min_severity: str = "URGENT"


@dataclass
class PcHealthSettings:
    """PC health sampling (psutil): sustained thresholds become PC_HEALTH incidents."""
    enabled: bool = True
    interval_seconds: float = 30.0
    cpu_percent: float = 90.0
    memory_percent: float = 90.0
    disk_free_percent: float = 5.0
    battery_percent: float = 20.0
    upload_kbps_min: float = 0.0          # 0 = off; evaluated only while LIVE
    sustain_seconds: float = 120.0
    recover_seconds: float = 60.0
    stall_after_seconds: float = 120.0    # monitor loop watchdog


@dataclass
class ClipsSettings:
    """Optional incident clips (GIF of the last seconds of redacted frames). Off by default."""
    enabled: bool = False
    seconds_before: float = 10.0
    fps: float = 1.0
    max_width: int = 640
    send: bool = True


@dataclass
class MemoryConfig:
    """Supermemory long-term memory (standalone PCs; in managed mode the hub syncs with its own key).
    The API key is entered locally (`studio-monitor memory set-key`) and lives only in the credential store."""
    enabled: bool = False
    namespace: str = ""                 # empty = "studio-monitor-<device_id prefix>"
    sync_resolved_incidents: bool = True
    sync_reports: bool = True
    retrieval_limit: int = 5


@dataclass
class HubConfig:
    """Fleet hub connection. The agent secret lives in the credential store (hub-agent/<device_id>)."""
    url: str = ""                       # e.g. https://hub.example.org ; empty = no hub
    enrolled: bool = False
    workspace_id: str = ""
    enrolled_utc: str = ""
    heartbeat_seconds: float = 15.0
    verify_tls: bool = True
    upload_evidence: bool = True        # redacted screenshots to the hub (still subject to privacy.send_screenshots)


@dataclass
class DeviceConfig:
    """Stable installation identity and fleet settings (used standalone and when managed by a hub)."""
    device_id: str = ""                 # uuid4, generated on first save; a copied install enrolls as a new device
    device_name: str = ""               # display name shown in the hub
    expected_account: str = ""          # configured expectation; observed account is tracked separately
    mode: str = "standalone"            # standalone | managed (hub owns Telegram delivery)
    schedule: dict = field(default_factory=dict)   # schedules.Schedule.to_dict()
    install_fingerprint: str = ""       # see install_fingerprint(); mismatch -> new device_id, enrollment dropped


@dataclass
class UiConfig:
    theme: str = "bootstrap-dark"   # ttkbootstrap theme name (bootstrap-dark | bootstrap-light)


CONFIG_VERSION = 5


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
    device: DeviceConfig = field(default_factory=DeviceConfig)
    detectors: DetectorsSettings = field(default_factory=DetectorsSettings)
    hub: HubConfig = field(default_factory=HubConfig)
    commands: CommandsConfig = field(default_factory=CommandsConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    pc_health: PcHealthSettings = field(default_factory=PcHealthSettings)
    clips: ClipsSettings = field(default_factory=ClipsSettings)
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
        cfg.device = DeviceConfig(**_known(DeviceConfig, data.get("device", {})))
        cfg.detectors = DetectorsSettings(**_known(DetectorsSettings, data.get("detectors", {})))
        cfg.hub = HubConfig(**_known(HubConfig, data.get("hub", {})))
        cfg.commands = CommandsConfig(**_known(CommandsConfig, data.get("commands", {})))
        cfg.escalation = EscalationConfig(**_known(EscalationConfig, data.get("escalation", {})))
        cfg.smtp = SmtpConfig(**_known(SmtpConfig, data.get("smtp", {})))
        cfg.memory = MemoryConfig(**_known(MemoryConfig, data.get("memory", {})))
        cfg.pc_health = PcHealthSettings(**_known(PcHealthSettings, data.get("pc_health", {})))
        cfg.clips = ClipsSettings(**_known(ClipsSettings, data.get("clips", {})))
        if cfg.device.mode not in ("standalone", "managed"):
            cfg.device.mode = "standalone"
        cfg.device = DeviceConfig(**_known(DeviceConfig, data.get("device", {})))
        if cfg.device.mode not in ("standalone", "managed"):
            cfg.device.mode = "standalone"
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

    def ensure_device_id(self, fingerprint: Optional[str] = None) -> str:
        """Create the device id on first use. When the stored install fingerprint no longer matches this
        machine/user (a copied installation), a fresh id is generated and any hub enrollment is dropped;
        the copied install must enroll as a new device with its own pairing code."""
        import uuid
        fp = fingerprint if fingerprint is not None else install_fingerprint(self.data_dir)
        self.identity_reset = False
        if self.device.device_id and self.device.install_fingerprint and self.device.install_fingerprint != fp:
            self.device.device_id = ""
            self.hub.enrolled, self.hub.workspace_id, self.hub.enrolled_utc = False, "", ""
            self.identity_reset = True
        if not self.device.device_id:
            self.device.device_id = str(uuid.uuid4())
        self.device.install_fingerprint = fp
        if not self.device.device_name:
            self.device.device_name = self.machine_label
        return self.device.device_id

    @property
    def schedule(self):
        from .schedules import Schedule
        return Schedule.from_dict(self.device.schedule)

    def save(self, path: Path) -> None:
        self.ensure_device_id()
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


def connection_rules_path(cfg: Optional[AppConfig] = None) -> Path:
    if cfg and cfg.detectors.rules_file:
        return Path(cfg.detectors.rules_file)
    return bundled_rules_path("connection_rules.json")


def live_rules_path(cfg: Optional[AppConfig] = None) -> Path:
    if cfg and cfg.activity.live_rules_file:
        return Path(cfg.activity.live_rules_file)
    return bundled_rules_path("live_state_rules.json")
