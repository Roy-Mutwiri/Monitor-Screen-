"""Hub settings from the environment (12-factor). Nothing secret is stored in
the database: Telegram bot tokens are referenced by environment variable name,
the admin password is a PBKDF2 hash, the session key is a random secret."""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field


def hash_password(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt).split("$", 2)[2], digest)


@dataclass
class HubSettings:
    database_url: str = "sqlite:///./hub.sqlite3"
    secret_key: str = ""                    # cookie signing; generated per process when empty (sessions won't survive restarts)
    admin_password_hash: str = ""           # pbkdf2$salt$digest; empty = dashboard login disabled
    admin_api_token: str = ""               # for CLI/automation: X-Admin-Token header
    heartbeat_interval: int = 15
    unreachable_after: int = 90
    pairing_ttl_seconds: int = 900
    evidence_dir: str = "./evidence"
    evidence_max_bytes: int = 5 * 1024 * 1024
    telegram_api_base: str = "https://api.telegram.org"
    workers_enabled: bool = True            # background sweeper + Telegram delivery loops
    public_url: str = ""
    default_workspace: str = "default"
    retention_days: int = 90
    supermemory_api_key: str = ""           # SUPERMEMORY_API_KEY on the hub only; never sent to agents
    supermemory_namespace_prefix: str = "studio-hub"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "HubSettings":
        e = env if env is not None else os.environ
        pw_hash = e.get("HUB_ADMIN_PASSWORD_HASH", "")
        if not pw_hash and e.get("HUB_ADMIN_PASSWORD"):
            pw_hash = hash_password(e["HUB_ADMIN_PASSWORD"])
        return cls(
            database_url=e.get("HUB_DATABASE_URL", cls.database_url),
            secret_key=e.get("HUB_SECRET_KEY", "") or secrets.token_urlsafe(32),
            admin_password_hash=pw_hash,
            admin_api_token=e.get("HUB_ADMIN_API_TOKEN", ""),
            heartbeat_interval=int(e.get("HUB_HEARTBEAT_INTERVAL", cls.heartbeat_interval)),
            unreachable_after=int(e.get("HUB_UNREACHABLE_AFTER", cls.unreachable_after)),
            pairing_ttl_seconds=int(e.get("HUB_PAIRING_TTL", cls.pairing_ttl_seconds)),
            evidence_dir=e.get("HUB_EVIDENCE_DIR", cls.evidence_dir),
            evidence_max_bytes=int(e.get("HUB_EVIDENCE_MAX_BYTES", cls.evidence_max_bytes)),
            telegram_api_base=e.get("HUB_TELEGRAM_API_BASE", cls.telegram_api_base),
            workers_enabled=e.get("HUB_WORKERS", "1") not in ("0", "false", "no"),
            public_url=e.get("HUB_PUBLIC_URL", ""),
            default_workspace=e.get("HUB_DEFAULT_WORKSPACE", cls.default_workspace),
            retention_days=int(e.get("HUB_RETENTION_DAYS", cls.retention_days)),
            supermemory_api_key=e.get("SUPERMEMORY_API_KEY", ""),
            supermemory_namespace_prefix=e.get("HUB_MEMORY_NAMESPACE_PREFIX", cls.supermemory_namespace_prefix),
        )
