"""Minimal Telegram Bot API client (stdlib only) with retry classification.

Network/5xx/429 problems raise :class:`DeliveryError` (retryable, honouring
``retry_after``); 4xx configuration problems raise a *permanent* DeliveryError.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .config import TelegramConfig
from .privacy import mask_secret
from .queue import DeliveryError

log = logging.getLogger(__name__)

# transport(url, data_bytes, headers, timeout) -> (status_code, body_bytes)
Transport = Callable[[str, Optional[bytes], dict, float], tuple[int, bytes]]


def _urllib_transport(url: str, data: Optional[bytes], headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DeliveryError(f"network error: {exc}") from exc


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
    def __init__(self, cfg: TelegramConfig, transport: Transport = _urllib_transport) -> None:
        self.cfg = cfg
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.cfg.bot_token and self.cfg.chat_id)

    def _url(self, method: str) -> str:
        return f"{self.cfg.api_base.rstrip('/')}/bot{self.cfg.bot_token}/{method}"

    def _call(self, method: str, fields: dict[str, str], files: Optional[dict] = None) -> dict:
        if not self.configured:
            raise DeliveryError("Telegram bot token / chat id not configured", permanent=True)
        if files:
            body, ctype = encode_multipart(fields, files)
            headers = {"Content-Type": ctype}
        else:
            body = urllib.parse.urlencode(fields).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        status, raw = self.transport(self._url(method), body, headers, self.cfg.timeout_seconds)
        try:
            data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if status == 200 and data.get("ok"):
            return data.get("result", {})
        desc = data.get("description") or f"HTTP {status}"
        # Never leak the token into logs/errors.
        desc = desc.replace(self.cfg.bot_token, mask_secret(self.cfg.bot_token))
        if status == 429:
            retry_after = float((data.get("parameters") or {}).get("retry_after", 5))
            raise DeliveryError(f"rate limited: {desc}", retry_after=retry_after)
        if status >= 500 or status == 0:
            raise DeliveryError(f"telegram server error: {desc}")
        if status in (400, 401, 403, 404):
            # Bad token / chat id / message: retrying will not help (unless it is a
            # transient "chat not found" during setup, which the user fixes by config).
            raise DeliveryError(f"telegram rejected request: {desc}", permanent=status in (401, 404))
        raise DeliveryError(f"telegram error: {desc}")

    # -- public API -------------------------------------------------------
    def get_me(self) -> dict:
        return self._call("getMe", {})

    def send_message(self, text: str, parse_mode: str = "HTML") -> dict:
        return self._call("sendMessage", {
            "chat_id": self.cfg.chat_id, "text": text, "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        })

    def send_photo(self, photo_path: str, caption: str, parse_mode: str = "HTML") -> dict:
        path = Path(photo_path)
        content = path.read_bytes()
        return self._call("sendPhoto", {
            "chat_id": self.cfg.chat_id, "caption": caption[:1024], "parse_mode": parse_mode,
        }, files={"photo": (path.name, content)})

    def send_document(self, doc_path: str, caption: str, parse_mode: str = "HTML") -> dict:
        path = Path(doc_path)
        return self._call("sendDocument", {
            "chat_id": self.cfg.chat_id, "caption": caption[:1024], "parse_mode": parse_mode,
        }, files={"document": (path.name, path.read_bytes())})


LATE_AFTER_SECONDS = 120.0


def late_delivery_note(payload: dict, now: float) -> str:
    """A visible stamp for alerts delivered well after they were generated
    (outage, retries): the reader must not mistake them for current events."""
    created = payload.get("created_at")
    if not created or now - float(created) < LATE_AFTER_SECONDS:
        return ""
    from .alerts import local_ts
    return f"\n<i>Delayed delivery: sent {local_ts(now)}, generated {local_ts(float(created))}.</i>"


def make_sender(client: TelegramClient, clock: Callable[[], float] = time.time) -> Callable[[dict, str], None]:
    """Adapter for :class:`DeliveryWorker`: payload has ``caption``/``text`` keys."""

    def _send(payload: dict, screenshot_path: str) -> None:
        note = late_delivery_note(payload, clock())
        caption = (payload.get("caption") or payload.get("text") or "") + note
        if screenshot_path and Path(screenshot_path).exists():
            try:
                client.send_photo(screenshot_path, caption[:1024])
                return
            except DeliveryError as exc:
                if exc.permanent or "network" in str(exc) or "rate limited" in str(exc) or "server" in str(exc):
                    raise
                log.warning("sendPhoto rejected (%s); falling back to text", exc)
        text = (payload.get("text") or payload.get("caption") or "") + note
        if screenshot_path and not Path(screenshot_path).exists():
            text += "\n(screenshot no longer available locally)"
        client.send_message(text)

    return _send
