"""Telegram bot configurations (up to :data:`MAX_BOTS`) and the registry that
keeps settings, the secure credential store and the delivery queue consistent.

A *bot* = token (sender, in the credential store) + destination chat/topic
(recipient, in settings) + event subscriptions. Tokens never live in settings,
SQLite, logs or history; settings hold a credential reference and a salted,
non-reversible fingerprint used only to reject duplicates.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

from .credentials import CredentialError, CredentialStore

MAX_BOTS = 10

# Event categories a bot can subscribe to. These are the only ones that exist.
CAT_RESTRICTIONS = "restrictions"
CAT_VERIFICATION = "verification"
CAT_STUDIO_OPENED = "studio_opened"
CAT_STUDIO_CLOSED = "studio_closed"
CAT_REMINDERS = "reminders"
CAT_BROADCAST = "broadcast_started"
CAT_HEALTH = "health"
CAT_TEST = "test"   # internal: explicit test notifications; not subscribable

EVENT_CATEGORIES: dict[str, str] = {
    CAT_RESTRICTIONS: "Restriction / content warnings (incl. suspensions and LIVE interruptions)",
    CAT_VERIFICATION: "Verification puzzles (manual attention)",
    CAT_STUDIO_OPENED: "Studio opened / already running",
    CAT_STUDIO_CLOSED: "Studio closed",
    CAT_REMINDERS: "Go-live (not-live) reminders",
    CAT_BROADCAST: "Broadcast started / already live (with screenshot)",
    CAT_HEALTH: "Monitoring health alerts (LOST / DEGRADED / RUNNING)",
}

TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")


class BotError(ValueError):
    pass


def utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc).isoformat(timespec="seconds")


def token_fingerprint(token: str, salt: str) -> str:
    """Salted HMAC-SHA256 of the token: non-reversible, only for duplicate checks."""
    return hmac.new(salt.encode("utf-8"), token.strip().encode("utf-8"), hashlib.sha256).hexdigest()


def validate_token_format(token: str) -> str:
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise BotError("The bot token must look like 123456789:ABCdef... as given by @BotFather. "
                       "It is not your Telegram password, phone code, API ID or API hash.")
    return token


def validate_chat_id(chat_id: str) -> str:
    chat_id = (chat_id or "").strip()
    if not chat_id:
        raise BotError("A destination chat ID is required (the recipient: a user, group, channel id or @publicname).")
    if CHAT_ID_RE.match(chat_id) or USERNAME_RE.match(chat_id):
        return chat_id
    raise BotError("Chat ID must be a number (negative for groups/channels, e.g. -1001234567890) or a public "
                   "@username.")


def validate_thread_id(value) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise BotError("Forum topic ID (message_thread_id) must be a positive whole number, or blank.")
    return int(text)


def validate_subscriptions(subs) -> list[str]:
    out = []
    for s in subs or []:
        if s not in EVENT_CATEGORIES:
            raise BotError(f"Unknown event category {s!r}; supported: {', '.join(EVENT_CATEGORIES)}")
        if s not in out:
            out.append(s)
    return out


@dataclass
class BotConfig:
    bot_id: str
    name: str
    chat_id: str
    thread_id: Optional[int] = None
    enabled: bool = True
    subscriptions: list[str] = field(default_factory=lambda: list(EVENT_CATEGORIES))
    credential_ref: str = ""
    token_fingerprint: str = ""
    verified_username: str = ""
    verified_bot_id: int = 0
    verified_utc: str = ""
    last_test_result: str = ""
    last_test_utc: str = ""
    created_utc: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BotConfig":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        known.setdefault("bot_id", str(uuid.uuid4()))
        known.setdefault("name", "Bot")
        known.setdefault("chat_id", "")
        bot = cls(**known)
        # Never keep an event type that does not exist.
        bot.subscriptions = [s for s in bot.subscriptions if s in EVENT_CATEGORIES]
        if bot.thread_id is not None:
            try:
                bot.thread_id = int(bot.thread_id)
            except (TypeError, ValueError):
                bot.thread_id = None
        return bot

    @property
    def destination(self) -> str:
        return self.chat_id + (f" / topic {self.thread_id}" if self.thread_id else "")

    def subscribed(self, category: str) -> bool:
        return self.enabled and category in self.subscriptions


@dataclass
class BotTarget:
    """Destination snapshot used when deliveries are created."""
    bot_id: str
    bot_name: str
    chat_id: str
    thread_id: Optional[int]


class BotRegistry:
    """Owns the bot list in settings plus the credential store.

    ``save`` persists settings; ``on_change`` lets the GUI/worker react
    (e.g. drop cached clients after a token rotation).
    """

    def __init__(self, cfg, store: CredentialStore, save: Callable[[], None],
                 queue=None) -> None:
        self.cfg = cfg
        self.store = store
        self.save = save
        self.queue = queue                     # optional DeliveryQueue for cancellations
        self.listeners: list[Callable[[str, str], None]] = []   # (action, bot_id)
        if not cfg.telegram.fingerprint_salt:
            cfg.telegram.fingerprint_salt = secrets.token_hex(16)

    # -- queries ------------------------------------------------------
    @property
    def bots(self) -> list[BotConfig]:
        return self.cfg.bots

    def get(self, bot_id: str) -> Optional[BotConfig]:
        return next((b for b in self.cfg.bots if b.bot_id == bot_id), None)

    def require(self, bot_id: str) -> BotConfig:
        bot = self.get(bot_id)
        if bot is None:
            raise BotError(f"No bot with id {bot_id}")
        return bot

    def by_name(self, name: str) -> Optional[BotConfig]:
        return next((b for b in self.cfg.bots if b.name.lower() == name.lower()), None)

    @property
    def count(self) -> int:
        return len(self.cfg.bots)

    @property
    def can_add(self) -> bool:
        return self.count < MAX_BOTS

    def targets(self, category: str) -> list[BotTarget]:
        """Enabled bots subscribed to ``category`` (or every enabled bot for tests)."""
        return [BotTarget(b.bot_id, b.name, b.chat_id, b.thread_id)
                for b in self.cfg.bots if b.enabled and (category == CAT_TEST or category in b.subscriptions)]

    def target_for(self, bot_id: str) -> BotTarget:
        b = self.require(bot_id)
        return BotTarget(b.bot_id, b.name, b.chat_id, b.thread_id)

    def token_for(self, bot_id: str) -> Optional[str]:
        return self.store.get(bot_id)

    def fingerprint(self, token: str) -> str:
        return token_fingerprint(token, self.cfg.telegram.fingerprint_salt)

    def _duplicate(self, fp: str, except_id: str = "") -> Optional[BotConfig]:
        return next((b for b in self.cfg.bots if b.token_fingerprint == fp and b.bot_id != except_id), None)

    # -- mutations -----------------------------------------------------
    def add(self, name: str, token: str, chat_id: str, thread_id=None, enabled: bool = True,
            subscriptions: Optional[list[str]] = None, verified: Optional[tuple[int, str]] = None) -> BotConfig:
        if not self.can_add:
            raise BotError(f"Maximum of {MAX_BOTS} bots reached (disabled bots count). Remove one to add another.")
        name = (name or "").strip()
        if not name:
            raise BotError("A bot name is required.")
        if self.by_name(name):
            raise BotError(f"A bot named {name!r} already exists.")
        token = validate_token_format(token)
        chat_id = validate_chat_id(chat_id)
        thread = validate_thread_id(thread_id)
        subs = validate_subscriptions(list(EVENT_CATEGORIES) if subscriptions is None else subscriptions)
        fp = self.fingerprint(token)
        dup = self._duplicate(fp)
        if dup:
            raise BotError(f"This token is already used by bot {dup.name!r}. Each bot needs its own token.")
        bot = BotConfig(bot_id=str(uuid.uuid4()), name=name, chat_id=chat_id, thread_id=thread, enabled=enabled,
                        subscriptions=subs, token_fingerprint=fp)
        if verified:
            bot.verified_bot_id, bot.verified_username = verified
            bot.verified_utc = utc_iso()
        # credential first, then settings; roll back the credential if settings cannot be written
        self.store.set(bot.bot_id, token)
        bot.credential_ref = self.store.reference(bot.bot_id)
        self.cfg.bots.append(bot)
        try:
            self.save()
        except Exception as exc:
            self.cfg.bots.remove(bot)
            try:
                self.store.delete(bot.bot_id)
            except CredentialError:
                pass
            raise BotError(f"Settings could not be saved; the bot was not added: {exc}") from exc
        self._notify("added", bot.bot_id)
        return bot

    def update(self, bot_id: str, *, name: Optional[str] = None, chat_id: Optional[str] = None,
               thread_id=..., enabled: Optional[bool] = None, subscriptions: Optional[list[str]] = None,
               new_token: Optional[str] = None, new_token_identity: Optional[tuple[int, str]] = None) -> BotConfig:
        """Edit settings; ``new_token`` empty/None keeps the current token.

        Destination edits apply to *future* events only: deliveries already
        queued keep their destination snapshot. Token rotation requires the new
        token's verified identity (``new_token_identity`` = (bot id, username)
        from getMe) to match the previously verified bot id.
        """
        bot = self.require(bot_id)
        if name is not None:
            name = name.strip()
            if not name:
                raise BotError("A bot name is required.")
            other = self.by_name(name)
            if other and other.bot_id != bot_id:
                raise BotError(f"A bot named {name!r} already exists.")
        if chat_id is not None:
            chat_id = validate_chat_id(chat_id)
        if thread_id is not ...:
            thread_id = validate_thread_id(thread_id)
        if subscriptions is not None:
            subscriptions = validate_subscriptions(subscriptions)
        token = None
        if new_token:
            token = validate_token_format(new_token)
            fp = self.fingerprint(token)
            dup = self._duplicate(fp, except_id=bot_id)
            if dup:
                raise BotError(f"This token is already used by bot {dup.name!r}.")
            if bot.verified_bot_id and new_token_identity and new_token_identity[0] != bot.verified_bot_id:
                raise BotError(
                    f"The new token belongs to a different bot (@{new_token_identity[1]}, id {new_token_identity[0]}); "
                    f"this configuration is for bot id {bot.verified_bot_id}. Add it as a new bot instead.")
            if bot.verified_bot_id and not new_token_identity:
                raise BotError("The new token must be validated (getMe) before it can replace the current one.")
        # apply
        old = BotConfig.from_dict(bot.to_dict())
        if name is not None:
            bot.name = name
        if chat_id is not None:
            bot.chat_id = chat_id
        if thread_id is not ...:
            bot.thread_id = thread_id
        if subscriptions is not None:
            bot.subscriptions = subscriptions
        if enabled is not None:
            bot.enabled = enabled
        if token:
            self.store.set(bot.bot_id, token)
            bot.token_fingerprint = self.fingerprint(token)
            bot.credential_ref = self.store.reference(bot.bot_id)
            if new_token_identity:
                bot.verified_bot_id, bot.verified_username = new_token_identity
                bot.verified_utc = utc_iso()
        try:
            self.save()
        except Exception as exc:
            idx = self.cfg.bots.index(bot)
            self.cfg.bots[idx] = old
            raise BotError(f"Settings could not be saved; changes were not applied: {exc}") from exc
        if enabled is False and old.enabled and self.queue is not None:
            self.queue.cancel_bot_pending(bot_id, "bot disabled")
        if token:
            self._notify("token", bot_id)
        self._notify("updated", bot_id)
        return bot

    def set_enabled(self, bot_id: str, enabled: bool) -> BotConfig:
        return self.update(bot_id, enabled=enabled)

    def remove(self, bot_id: str) -> BotConfig:
        """Cancel pending deliveries, drop settings, delete the credential.
        Historical delivery rows are kept (they never held the token)."""
        bot = self.require(bot_id)
        if self.queue is not None:
            self.queue.cancel_bot_pending(bot_id, "bot removed")
        self.cfg.bots.remove(bot)
        try:
            self.save()
        except Exception as exc:
            self.cfg.bots.append(bot)
            raise BotError(f"Settings could not be saved; the bot was not removed: {exc}") from exc
        try:
            self.store.delete(bot_id)
        except CredentialError as exc:
            # Settings no longer reference it; report so the user can clean up manually.
            raise BotError(f"Bot removed, but its stored credential could not be deleted: {exc}") from exc
        finally:
            self._notify("removed", bot_id)
        return bot

    def record_validation(self, bot_id: str, tg_id: int, username: str) -> None:
        bot = self.require(bot_id)
        bot.verified_bot_id, bot.verified_username, bot.verified_utc = tg_id, username, utc_iso()
        bot.last_test_result = f"validated @{username}"
        bot.last_test_utc = bot.verified_utc
        self.save()

    def record_test(self, bot_id: str, result: str) -> None:
        bot = self.require(bot_id)
        bot.last_test_result = result[:200]
        bot.last_test_utc = utc_iso()
        self.save()

    # -- migration ------------------------------------------------------
    def migrate_single_bot(self, token: str, chat_id: str, name: str = "Default Bot") -> Optional[BotConfig]:
        """Move a legacy single-bot token/chat into the registry (once)."""
        token = (token or "").strip()
        if not token or not chat_id:
            return None
        try:
            fp = self.fingerprint(validate_token_format(token))
        except BotError:
            return None
        if self._duplicate(fp):
            return None
        if self.by_name(name):
            name = f"{name} {secrets.token_hex(2)}"
        return self.add(name, token, chat_id)

    def _notify(self, action: str, bot_id: str) -> None:
        for cb in list(self.listeners):
            try:
                cb(action, bot_id)
            except Exception:
                pass


def mask_token(token: str) -> str:
    if not token:
        return ""
    head = token.split(":", 1)[0]
    return f"{head[:4]}…:••••••"
