"""Compose Telegram alert text for an incident."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional

from . import SOURCE_LABEL
from .incidents import Incident
from .privacy import bounded_text

MANUAL_ATTENTION = "Manual attention required"

_ICONS = {
    "verification_puzzle": "\U0001F9E9",  # puzzle piece
    "account_suspension": "\U0001F6D1",   # stop sign
    "live_interruption": "\U0001F4F4",    # phone off
    "restriction_notice": "⛔",       # no entry
    "content_warning": "⚠️",    # warning
}


def _ts(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts).astimezone()
    except (OSError, OverflowError, ValueError):  # Windows rejects timestamps near the epoch
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def format_alert(incident: Incident, machine_label: str, max_text: int = 400,
                 screenshot_attached: bool = True, reason: str = "",
                 capture_method: Optional[str] = None) -> dict:
    """Return ``{"caption": html, "text": html}``.

    ``caption`` is used with the screenshot (Telegram caps captions at 1024
    chars); ``text`` is the standalone fallback.
    """
    icon = _ICONS.get(incident.category, "\U0001F6A8")
    headline = f"{icon} <b>{html.escape(SOURCE_LABEL)}</b> — {html.escape(incident.label)}"
    if incident.manual_attention:
        headline = f"❗ <b>{MANUAL_ATTENTION}</b>\n{headline}"
    where = "separate dialog" if incident.is_dialog else "main window"
    if incident.window_title:
        where += f' "{html.escape(incident.window_title)}"'
    detected = html.escape(bounded_text(incident.text, max_text)) or "(no text)"
    lines = [
        headline,
        f"<b>Source:</b> {html.escape(SOURCE_LABEL)}",
        f"<b>Category:</b> {html.escape(incident.category)}",
        f"<b>Detected text:</b> <i>{detected}</i>",
        f"<b>Where:</b> {where}",
        f"<b>Time:</b> {html.escape(_ts(incident.last_alerted or incident.last_seen))}",
        f"<b>Machine:</b> {html.escape(machine_label)}",
        f"<b>Incident ID:</b> <code>{html.escape(incident.incident_id)}</code>",
    ]
    if incident.manual_attention:
        lines.append("Studio is waiting for a human to complete this step. The monitor does not interact with Studio.")
    if reason:
        lines.append(f"<b>Note:</b> {html.escape(reason)}")
    if not screenshot_attached:
        lines.append("<i>Screenshot not attached (privacy setting).</i>")
    text = "\n".join(lines)
    caption = text if len(text) <= 1024 else text[:1020] + "…"
    return {"caption": caption, "text": text}


def format_status_alert(status: str, reason: str, machine_label: str, ts: float) -> str:
    return "\n".join([
        f"ℹ️ <b>{html.escape(SOURCE_LABEL)}</b> monitor status: <b>{html.escape(status)}</b>",
        f"<b>Reason:</b> {html.escape(reason) or '-'}",
        f"<b>Time:</b> {html.escape(_ts(ts))}",
        f"<b>Machine:</b> {html.escape(machine_label)}",
    ])
