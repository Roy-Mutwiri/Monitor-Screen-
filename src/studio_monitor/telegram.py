"""Minimal Telegram Bot API client (stdlib only) with retry classification.

Network/5xx/429 problems raise :class:`DeliveryError` (retryable, honouring
``retry_after``); 4xx configuration problems raise a *permanent* DeliveryError.

Every error message and log line passes through :func:`sanitize` so the bot
token (which Telegram puts in the request URL) never leaks.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .config import TelegramConfig
from .queue import DeliveryError

log = logging.getLogger(__name__)

# transport(url, data_bytes, headers, timeout) -> (status_code, body_bytes)
Transport = Callable[[str, Optional[bytes], dict, float], tuple[int, bytes]]

_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


def sanitize(text: str, token: str = "") -> str:
    """Remove bot tokens (and Telegram URLs carrying them) from any text."""
    if not text:
        return text
    if token:
        text = text.replace(token, "[REDACTED]")
    text = _TOKEN_IN_URL.sub("/bot[REDACTED]", text)
    return _BARE_TOKEN.sub("[REDACTED]", text)


class TokenRedactingFilter(logging.Filter):
    """Attach to the root logger so no handler can write a token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = sanitize(msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def _urllib_transport(url: str, data: Optional[bytes], headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except TimeoutError as exc:
        # Ambiguous: Telegram may have accepted the message. A retry can duplicate it.
        raise DeliveryError(f"timeout (delivery outcome unknown; a retry may duplicate): {sanitize(str(exc))}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DeliveryError(f"network error: {sanitize(str(exc))}") from exc


def encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "----StudioMonitor" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content) in files.items():
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class TelegramClient:
    def __init__(self, cfg: TelegramConfig, token: str, chat_id: str = "", thread_id: Optional[int] = None,
                 transport: Transport = _urllib_transport) -> None:
        self.cfg = cfg
        self.token = (token or "").strip()
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"{self.cfg.api_base.rstrip('/')}/bot{self.token}/{method}"

    def _call(self, method: str, fields: dict[str, str], files: Optional[dict] = None) -> dict:
        if not self.configured:
            raise DeliveryError("bot token not available", permanent=True)
        if files:
            body, ctype = encode_multipart(fields, files)
            headers = {"Content-Type": ctype}
        else:
            body = urllib.parse.urlencode(fields).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            status, raw = self.transport(self._url(method), body, headers, self.cfg.timeout_seconds)
        except DeliveryError as exc:
            raise DeliveryError(sanitize(str(exc), self.token), exc.retry_after, exc.permanent) from None
        except Exception as exc:
            raise DeliveryError(f"transport failure: {sanitize(str(exc), self.token)}") from None
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if status == 200 and data.get("ok"):
            return data.get("result", {})
        desc = sanitize(data.get("description") or f"HTTP {status}", self.token)
        if status == 429:
            retry_after = float((data.get("parameters") or {}).get("retry_after", 5))
            raise DeliveryError(f"rate limited: {desc}", retry_after=retry_after)
        if status >= 500 or status == 0:
            raise DeliveryError(f"telegram server error: {desc}")
        if status == 401:
            raise DeliveryError(f"token rejected by Telegram: {desc}", permanent=True)
        if status == 403:
            raise DeliveryError(f"destination refused (bot blocked, not a member, or no permission): {desc}",
                                permanent=True)
        if status == 400 and ("chat not found" in desc.lower() or "thread" in desc.lower()):
            raise DeliveryError(f"destination problem (token is fine): {desc}", permanent=True)
        if status in (400, 404):
            raise DeliveryError(f"telegram rejected request: {desc}", permanent=status == 404)
        raise DeliveryError(f"telegram error: {desc}")

    # -- public API -------------------------------------------------------
    def get_me(self) -> dict:
        """Token check only; sends nothing to any chat."""
        return self._call("getMe", {})

    def _dest(self, fields: dict) -> dict:
        fields["chat_id"] = self.chat_id
        if self.thread_id:
            fields["message_thread_id"] = str(self.thread_id)
        return fields

    def send_message(self, text: str, parse_mode: str = "HTML") -> dict:
        return self._call("sendMessage", self._dest({
            "text": text, "parse_mode": parse_mode, "disable_web_page_preview": "true",
        }))

    def send_photo(self, photo_path: str, caption: str, parse_mode: str = "HTML") -> dict:
        path = Path(photo_path)
        content = path.read_bytes()
        return self._call("sendPhoto", self._dest({"caption": caption[:1024], "parse_mode": parse_mode}),
                          files={"photo": (path.name, content)})


LATE_AFTER_SECONDS = 120.0


def late_delivery_note(payload: dict, now: float) -> str:
    """A visible stamp for alerts delivered well after they were generated
    (outage, retries): the reader must not mistake them for current events."""
    created = payload.get("created_at")
    if not created or now - float(created) < LATE_AFTER_SECONDS:
        return ""
    from .alerts import local_ts
    return f"\n<i>Delayed delivery: sent {local_ts(now)}, generated {local_ts(float(created))}.</i>"


def deliver(client: TelegramClient, payload: dict, screenshot_path: str, clock: Callable[[], float] = time.time) -> dict:
    """Send one payload (photo with caption when the evidence file exists,
    otherwise text). Returns the Telegram result (has ``message_id``)."""
    note = late_delivery_note(payload, clock())
    caption = (payload.get("caption") or payload.get("text") or "") + note
    if screenshot_path and Path(screenshot_path).exists():
        try:
            return client.send_photo(screenshot_path, caption[:1024])
        except DeliveryError as exc:
            msg = str(exc)
            if exc.permanent or "network" in msg or "rate limited" in msg or "server" in msg or "timeout" in msg:
                raise
            log.warning("sendPhoto rejected (%s); falling back to text", msg)
    text = (payload.get("text") or payload.get("caption") or "") + note
    if screenshot_path and not Path(screenshot_path).exists():
        text += "\n(screenshot no longer available locally)"
    return client.send_message(text)


class ClientFactory:
    """Builds (and caches) one client per bot using the credential store."""

    def __init__(self, cfg: TelegramConfig, token_lookup: Callable[[str], Optional[str]],
                 transport: Transport = _urllib_transport) -> None:
        self.cfg = cfg
        self.token_lookup = token_lookup
        self.transport = transport
        self._tokens: dict[str, str] = {}

    def invalidate(self, bot_id: str = "") -> None:
        if bot_id:
            self._tokens.pop(bot_id, None)
        else:
            self._tokens.clear()

    def token(self, bot_id: str) -> Optional[str]:
        tok = self._tokens.get(bot_id)
        if tok is None:
            tok = self.token_lookup(bot_id)
            if tok:
                self._tokens[bot_id] = tok
        return tok

    def client(self, bot_id: str, chat_id: str, thread_id: Optional[int]) -> Optional[TelegramClient]:
        tok = self.token(bot_id)
        if not tok:
            return None
        return TelegramClient(self.cfg, tok, chat_id, thread_id, self.transport)

    def validation_client(self, token: str) -> TelegramClient:
        return TelegramClient(self.cfg, token, transport=self.transport)
