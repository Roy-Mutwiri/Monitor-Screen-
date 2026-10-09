"""SMTP backup route: plain-text e-mail when Telegram delivery of an URGENT
event has definitively failed. Password lives in the credential store
(``smtp/<profile>``); the sender is injectable for tests."""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable, Optional

from .credentials import CredentialStore

log = logging.getLogger(__name__)
SMTP_CRED_KEY = "smtp/backup"


@dataclass
class SmtpSettings:
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    starttls: bool = True
    timeout_seconds: float = 20.0
    min_severity: str = "URGENT"          # only urgent events by default


class EmailBackupError(Exception):
    pass


def _smtp_send(settings: SmtpSettings, password: str, msg: EmailMessage) -> None:  # pragma: no cover - network
    if settings.starttls:
        with smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_seconds) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            if settings.username:
                s.login(settings.username, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(settings.host, settings.port, timeout=settings.timeout_seconds,
                              context=ssl.create_default_context()) as s:
            if settings.username:
                s.login(settings.username, password)
            s.send_message(msg)


class EmailBackup:
    def __init__(self, settings: SmtpSettings, store: Optional[CredentialStore],
                 sender: Callable[[SmtpSettings, str, EmailMessage], None] = _smtp_send) -> None:
        self.settings = settings
        self.store = store
        self.sender = sender
        self.sent: int = 0
        self.last_error: str = ""

    @property
    def configured(self) -> bool:
        s = self.settings
        return s.enabled and bool(s.host and s.from_addr and s.to_addrs)

    def send(self, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        password = ""
        if self.settings.username and self.store is not None:
            password = self.store.get(SMTP_CRED_KEY) or ""
            if not password:
                self.last_error = "SMTP password missing from the credential store"
                log.warning(self.last_error)
                return False
        msg = EmailMessage()
        msg["Subject"] = subject[:200]
        msg["From"] = self.settings.from_addr
        msg["To"] = ", ".join(self.settings.to_addrs)
        msg.set_content(body)
        try:
            self.sender(self.settings, password, msg)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("email backup failed: %s", self.last_error)
            return False
        self.sent += 1
        self.last_error = ""
        return True
