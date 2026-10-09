"""Telegram command handling shared by the agent (standalone mode) and the hub.

* ``CommandRouter`` parses ``/status /screenshot /sessions /ack /snooze /report /help``
  and inline-button callbacks, authorises by chat id, rate-limits, and asks a
  ``CommandBackend`` for the answer. Commands are predefined operations only;
  no text is ever executed.
* ``UpdatePoller`` is the single getUpdates consumer for one bot: offset is
  persisted after processing (at-least-once, deduplicated by update_id), a
  local lease prevents two pollers in one installation, and Telegram's
  409 Conflict (another consumer elsewhere) backs off and is reported.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .queue import DeliveryError
from .telegram import TelegramClient, sanitize

log = logging.getLogger(__name__)

COMMANDS = ("status", "screenshot", "sessions", "ack", "snooze", "report", "help")
HELP = ("<b>Commands</b>\n"
        "/status — Studio, broadcast, account, capture and hub state\n"
        "/screenshot — latest redacted Studio frame\n"
        "/sessions — recent Studio sessions and broadcasts\n"
        "/ack INCIDENT — acknowledge (pauses reminders, does not resolve)\n"
        "/snooze INCIDENT MINUTES — pause reminders for a while\n"
        "/report — summary of the current session\n"
        "Buttons under incident alerts do the same.")


class CommandBackend(Protocol):
    def status(self) -> str: ...
    def screenshot(self) -> tuple[Optional[str], str]: ...
    def sessions(self, limit: int) -> str: ...
    def ack(self, incident_id: str, actor: str) -> str: ...
    def snooze(self, incident_id: str, minutes: int, actor: str) -> str: ...
    def report(self) -> str: ...


@dataclass
class Reply:
    chat_id: str
    text: str = ""
    photo_path: str = ""
    thread_id: Optional[int] = None
    reply_to: Optional[int] = None
    callback_id: str = ""          # answerCallbackQuery target
    callback_text: str = ""


def incident_keyboard(incident_id: str, with_screenshot: bool = True) -> dict:
    rows = [[{"text": "Acknowledge", "callback_data": f"ack:{incident_id}"},
             {"text": "Snooze 30 min", "callback_data": f"snooze:{incident_id}:30"}]]
    if with_screenshot:
        rows.append([{"text": "Screenshot now", "callback_data": "shot"}])
    return {"inline_keyboard": rows}


_CMD_RE = re.compile(r"^/([a-zA-Z_]+)(?:@\w+)?(?:\s+(.*))?$", re.S)
_INC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-:.]{2,80}$")


def parse_command(text: str) -> Optional[tuple[str, list[str]]]:
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None
    return m.group(1).lower(), (m.group(2) or "").split()


class CommandRouter:
    def __init__(self, backend: CommandBackend, allowed_chats: set[str], on_audit: Optional[Callable[[str], None]] = None,
                 rate_per_minute: int = 12, clock: Callable[[], float] = time.time, actor_prefix: str = "telegram") -> None:
        self.backend = backend
        self.allowed = {str(c) for c in allowed_chats}
        self.on_audit = on_audit or (lambda m: log.info(m))
        self.rate = rate_per_minute
        self.clock = clock
        self.actor_prefix = actor_prefix
        self._hits: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    def _allowed(self, chat_id: str) -> bool:
        return str(chat_id) in self.allowed

    def _rate_ok(self, chat_id: str) -> bool:
        now = self.clock()
        hits = [t for t in self._hits.get(chat_id, []) if now - t < 60]
        hits.append(now)
        self._hits[chat_id] = hits
        return len(hits) <= self.rate

    @staticmethod
    def _actor(user: dict, prefix: str) -> str:
        name = user.get("username") or " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or str(user.get("id", "?"))
        return f"{prefix}:{name}"[:120]

    def handle_update(self, update: dict) -> list[Reply]:
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        msg = update.get("message") or update.get("edited_message")
        if not msg or not isinstance(msg.get("text"), str):
            return []
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        thread_id = msg.get("message_thread_id")
        parsed = parse_command(msg["text"])
        if parsed is None:
            return []
        cmd, args = parsed
        if not self._allowed(chat_id):
            self.on_audit(f"ignored /{cmd} from unauthorised chat {chat_id}")
            return []
        if not self._rate_ok(chat_id):
            self.on_audit(f"rate limit: /{cmd} from chat {chat_id} dropped")
            return []
        actor = self._actor(msg.get("from") or {}, self.actor_prefix)
        self.on_audit(f"command /{cmd} {' '.join(args)} from {actor}")
        text, photo = self.execute(cmd, args, actor)
        return [Reply(chat_id, text, photo, thread_id, msg.get("message_id"))]

    def _handle_callback(self, cq: dict) -> list[Reply]:
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        cb_id = str(cq.get("id", ""))
        data = str(cq.get("data", ""))
        if not self._allowed(chat_id):
            self.on_audit(f"ignored button '{data}' from unauthorised chat {chat_id}")
            return [Reply(chat_id, callback_id=cb_id, callback_text="Not authorised")]
        if not self._rate_ok(chat_id):
            return [Reply(chat_id, callback_id=cb_id, callback_text="Too many requests, try again shortly")]
        actor = self._actor(cq.get("from") or {}, self.actor_prefix)
        parts = data.split(":")
        if parts[0] == "ack" and len(parts) == 2:
            cmd, args = "ack", [parts[1]]
        elif parts[0] == "snooze" and len(parts) == 3:
            cmd, args = "snooze", [parts[1], parts[2]]
        elif parts[0] == "shot":
            cmd, args = "screenshot", []
        else:
            return [Reply(chat_id, callback_id=cb_id, callback_text="Unknown button")]
        self.on_audit(f"button {data} from {actor}")
        text, photo = self.execute(cmd, args, actor)
        return [Reply(chat_id, text, photo, msg.get("message_thread_id"), msg.get("message_id"), cb_id,
                      callback_text=_plain(text)[:180])]

    def execute(self, cmd: str, args: list[str], actor: str) -> tuple[str, str]:
        try:
            if cmd == "status":
                return self.backend.status(), ""
            if cmd == "screenshot":
                path, caption = self.backend.screenshot(args[0]) if args else self.backend.screenshot()
                return caption, path or ""
            if cmd == "sessions":
                return self.backend.sessions(10), ""
            if cmd == "report":
                return self.backend.report(), ""
            if cmd == "ack":
                if len(args) != 1 or not _INC_RE.match(args[0]):
                    return "Usage: /ack INCIDENT_ID", ""
                return self.backend.ack(args[0], actor), ""
            if cmd == "snooze":
                if len(args) != 2 or not _INC_RE.match(args[0]) or not args[1].isdigit():
                    return "Usage: /snooze INCIDENT_ID MINUTES", ""
                minutes = max(1, min(int(args[1]), 24 * 60))
                return self.backend.snooze(args[0], minutes, actor), ""
            if cmd in ("help", "start"):
                return HELP, ""
            return f"Unknown command /{cmd}.\n\n{HELP}", ""
        except Exception as exc:  # the backend must never take the poller down
            log.exception("command failed")
            return f"Command failed: {sanitize(str(exc))[:200]}", ""


def _plain(text: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


# ---------------------------------------------------------------------- poller

class UpdatePoller:
    """getUpdates consumer for one bot. ``state_get/state_set`` persist the
    offset and the consumer lease (e.g. DeliveryQueue.get_state/set_state)."""

    def __init__(self, client: TelegramClient, router: CommandRouter, state_get: Callable[[str, object], object],
                 state_set: Callable[[str, object], None], bot_id: str, owner: str, on_event: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time, long_poll_seconds: int = 20, lease_seconds: float = 90.0) -> None:
        self.client, self.router = client, router
        self.state_get, self.state_set = state_get, state_set
        self.bot_id, self.owner = bot_id, owner
        self.on_event = on_event or (lambda m: log.info(m))
        self.clock = clock
        self.long_poll = long_poll_seconds
        self.lease_seconds = lease_seconds
        self.backoff_until = 0.0
        self.conflicts = 0
        self.handled = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def offset_key(self) -> str:
        return f"tg_updates_offset:{self.bot_id}"

    @property
    def lease_key(self) -> str:
        return f"tg_consumer_lease:{self.bot_id}"

    def acquire_lease(self) -> bool:
        now = self.clock()
        lease = self.state_get(self.lease_key, None) or {}
        if lease and lease.get("owner") != self.owner and float(lease.get("expires", 0)) > now:
            return False
        self.state_set(self.lease_key, {"owner": self.owner, "expires": now + self.lease_seconds})
        return True

    def release_lease(self) -> None:
        lease = self.state_get(self.lease_key, None) or {}
        if lease.get("owner") == self.owner:
            self.state_set(self.lease_key, {"owner": "", "expires": 0})

    def poll_once(self, timeout: Optional[int] = None) -> int:
        """One getUpdates round. Returns the number of updates handled."""
        now = self.clock()
        if now < self.backoff_until:
            return 0
        if not self.acquire_lease():
            self.backoff_until = now + 30
            return 0
        offset = int(self.state_get(self.offset_key, 0) or 0)
        try:
            updates = self.client.get_updates(offset, self.long_poll if timeout is None else timeout)
        except DeliveryError as exc:
            msg = str(exc)
            if "409" in msg or "Conflict" in msg or "terminated by other getUpdates" in msg:
                self.conflicts += 1
                self.backoff_until = now + 60
                self.on_event(f"another consumer is polling this bot (Telegram 409); commands paused 60 s (#{self.conflicts})")
            else:
                self.backoff_until = now + (exc.retry_after or 10)
                log.debug("getUpdates failed: %s", msg)
            return 0
        handled = 0
        last_id = offset - 1
        for upd in updates:
            uid = int(upd.get("update_id", 0))
            if uid < offset:
                continue                                      # already processed (at-least-once replay)
            try:
                for reply in self.router.handle_update(upd):
                    self._send(reply)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("update %s failed: %s", uid, exc)
            last_id = max(last_id, uid)
            handled += 1
        if handled:
            self.state_set(self.offset_key, last_id + 1)
            self.handled += handled
        return handled

    def _send(self, r: Reply) -> None:
        if r.callback_id:
            try:
                self.client.answer_callback(r.callback_id, r.callback_text)
            except DeliveryError as exc:
                log.debug("answerCallbackQuery failed: %s", exc)
        if not r.text and not r.photo_path:
            return
        c = self.client.for_destination(r.chat_id, r.thread_id)
        try:
            if r.photo_path:
                c.send_photo(r.photo_path, r.text[:1024], reply_to=r.reply_to)
            else:
                c.send_message(r.text, reply_to=r.reply_to)
        except DeliveryError as exc:
            self.on_event(f"command reply failed: {sanitize(str(exc))}")

    # ------------------------------------------------------------------ thread
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover
                log.exception("poller failed: %s", exc)
            if self.clock() < self.backoff_until:
                self._stop.wait(min(5.0, max(0.5, self.backoff_until - self.clock())))

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"tg-commands-{self.bot_id[:8]}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.long_poll + 5)
        try:
            self.release_lease()
        except Exception:  # pragma: no cover
            pass
