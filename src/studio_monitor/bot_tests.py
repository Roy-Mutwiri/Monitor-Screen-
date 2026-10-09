"""Explicit bot validation / test notifications.

Validation calls ``getMe`` only (nothing is sent). A test notification goes to
the selected bot's configured destination only, with an explicit caption and a
clearly labelled *synthetic* image: the desktop is never captured to test
credentials.
"""
from __future__ import annotations

import html
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

from .alerts import local_ts
from .bots import CAT_TEST, BotRegistry
from .config import AppConfig
from .queue import KIND_TEST, DeliveryError, DeliveryQueue, DeliveryWorker
from .telegram import ClientFactory, deliver, sanitize


def validate_token(factory: ClientFactory, token: str) -> dict:
    """getMe for a raw token. Returns {id, username, first_name}. Raises DeliveryError."""
    me = factory.validation_client(token).get_me()
    return {"id": int(me.get("id", 0)), "username": str(me.get("username", "")),
            "first_name": str(me.get("first_name", ""))}


def validate_bot(factory: ClientFactory, registry: BotRegistry, bot_id: str) -> dict:
    tok = registry.token_for(bot_id)
    if not tok:
        raise DeliveryError("no token stored for this bot in the credential store", permanent=True)
    info = validate_token(factory, tok)
    registry.record_validation(bot_id, info["id"], info["username"])
    return info


def synthetic_test_image(path: Path, bot_name: str, machine_label: str, ts: Optional[float] = None) -> Path:
    """A generated picture that cannot be mistaken for a Studio capture."""
    ts = time.time() if ts is None else ts
    img = Image.new("RGB", (900, 420), (28, 32, 48))
    d = ImageDraw.Draw(img)
    try:
        big = ImageFont.truetype("arial.ttf", 44)
        small = ImageFont.truetype("arial.ttf", 24)
    except OSError:  # pragma: no cover
        big = small = ImageFont.load_default()
    d.rectangle((0, 0, 900, 70), fill=(255, 193, 7))
    d.text((24, 12), "SYNTHETIC TEST IMAGE", fill=(0, 0, 0), font=big)
    lines = [
        "Monitor Screen - Telegram bot test",
        f"Bot: {bot_name}",
        f"Machine: {machine_label}",
        f"Generated: {local_ts(ts)}",
        "This is NOT a TikTok LIVE Studio capture.",
        "No desktop content was captured or sent.",
    ]
    y = 100
    for line in lines:
        d.text((24, y), line, fill=(235, 235, 235), font=small)
        y += 44
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.png")
    img.save(tmp, format="PNG")
    tmp.replace(path)
    return path


def enqueue_test(queue: DeliveryQueue, registry: BotRegistry, cfg: AppConfig, bot_id: str,
                 clock: Callable[[], float] = time.time) -> str:
    """Create a test event with exactly one delivery (the selected bot)."""
    bot = registry.require(bot_id)
    now = clock()
    event_id = f"TEST-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"
    shot = ""
    if cfg.privacy.send_screenshots:
        shot = str(synthetic_test_image(cfg.activity_screenshots_dir / f"{event_id}.png", bot.name,
                                        cfg.machine_label, now))
    text = "\n".join([
        "\U0001F9EA <b>TEST NOTIFICATION</b> from Monitor Screen",
        f"Bot: {html.escape(bot.name)}",
        f"Destination: <code>{html.escape(bot.destination)}</code>",
        f"PC: {html.escape(cfg.machine_label)}",
        f"Time: {local_ts(now)}",
        "The attached image is synthetic. Nothing from the desktop was captured." if shot
        else "Text-only test (screenshots disabled in privacy settings).",
    ])
    queue.create_event(event_id, KIND_TEST, CAT_TEST, {"caption": text, "text": text, "created_at": now}, shot,
                       [registry.target_for(bot_id)], label=f"Test -> {bot.name}")
    return event_id


def deliver_test_now(queue: DeliveryQueue, registry: BotRegistry, factory: ClientFactory, event_id: str,
                     clock: Callable[[], float] = time.time) -> str:
    """Deliver the test event synchronously (used when no worker is running).
    Returns a human-readable result and records it on the bot."""
    ds = queue.deliveries_for(event_id)
    if not ds:
        return "no delivery row found"
    d = ds[0]
    client = factory.client(d.bot_id, d.chat_id, d.thread_id)
    try:
        if client is None:
            raise DeliveryError("no token stored for this bot", permanent=True)
        result = deliver(client, d.payload, d.evidence_path, clock)
        queue.mark_sent(d.id, result.get("message_id") if isinstance(result, dict) else None)
        msg = f"test delivered (message id {result.get('message_id', '?')})"
    except DeliveryError as exc:
        queue.mark_failed(d.id, str(exc), exc.retry_after, exc.permanent)
        msg = f"test failed: {sanitize(str(exc))}"
    registry.record_test(d.bot_id, msg)
    return msg
