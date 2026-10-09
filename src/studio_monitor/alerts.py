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


def local_ts(ts: float) -> str:
    """Local time with explicit UTC offset, e.g. ``2026-10-09 14:03:11 UTC+03:00``."""
    try:
        dt = datetime.fromtimestamp(ts).astimezone()
    except (OSError, OverflowError, ValueError):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    off = dt.strftime("%z")
    off = f"UTC{off[:3]}:{off[3:]}" if off else "UTC"
    return f"{dt:%Y-%m-%d %H:%M:%S} {off}"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {seconds % 60:02d}s"


def _finish(lines: list[str], ts: float) -> dict:
    text = "\n".join(lines)
    caption = text if len(text) <= 1024 else text[:1020] + "…"
    return {"caption": caption, "text": text, "created_at": ts}


def format_studio_opened(machine_label: str, ts: float, screenshot_attached: bool,
                         timeout_seconds: float = 0.0) -> dict:
    lines = [
        "<b>TIKTOK LIVE STUDIO OPENED</b>",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
        f"{html.escape(SOURCE_LABEL)} is now running.",
    ]
    if not screenshot_attached:
        lines.append(f"<i>Screenshot unavailable: no usable capture of the Studio window within "
                     f"{int(timeout_seconds)} s.</i>")
    return _finish(lines, ts)


def format_studio_already_running(machine_label: str, ts: float, screenshot_attached: bool) -> dict:
    lines = [
        "<b>TIKTOK LIVE STUDIO ALREADY RUNNING</b>",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
        "Studio already running — monitoring started.",
    ]
    if not screenshot_attached:
        lines.append("<i>Screenshot unavailable at monitor start.</i>")
    return _finish(lines, ts)


def format_studio_closed(machine_label: str, ts: float, screenshot_captured_at: Optional[float]) -> dict:
    lines = [
        "<b>TIKTOK LIVE STUDIO CLOSED</b>",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
        f"{html.escape(SOURCE_LABEL)} has closed.",
    ]
    if screenshot_captured_at is not None:
        lines.append("Image: last available screenshot before closure.")
        lines.append(f"Screenshot captured: {local_ts(screenshot_captured_at)}")
    else:
        lines.append("<i>No screenshot available from before closure.</i>")
    return _finish(lines, ts)


def format_not_live_reminder(machine_label: str, ts: float, threshold_minutes: float, offline_seconds: float,
                             episode_id: str, sequence: int = 1, max_count: int = 1,
                             screenshot_attached: bool = True, rules_verified: bool = True) -> dict:
    hours = threshold_minutes / 60.0
    period = f"{int(hours)} hour{'s' if int(hours) != 1 else ''}" if hours >= 1 and hours == int(hours) \
        else f"{int(threshold_minutes)} minutes"
    lines = [
        "<b>TIME TO GO LIVE</b>",
        f"PC: {html.escape(machine_label)}",
        f"{html.escape(SOURCE_LABEL)} has been confirmed not live for at least {period}.",
        "Open your broadcast setup and go live when ready.",
        f"Confirmed offline time: {format_duration(offline_seconds)} (episode <code>{html.escape(episode_id)}</code>)",
        f"Generated: {local_ts(ts)}",
    ]
    if sequence > 1:
        lines.append(f"Repeat reminder {sequence} of up to {max_count + 1}.")
    if not screenshot_attached:
        lines.append("<i>Screenshot unavailable: no fresh capture of the Studio window.</i>")
    if not rules_verified:
        lines.append("<i>Live-state rules are unverified (not yet calibrated on real Studio screenshots).</i>")
    return _finish(lines, ts)


def format_broadcast_started(machine_label: str, ts: float, account_label: str = "", after_gap: bool = False,
                             gap_seconds: float = 0.0, screenshot_attached: bool = True, rules_verified: bool = True) -> dict:
    lines = [
        "<b>TIKTOK LIVE STUDIO HAS GONE LIVE</b>",
        f"PC: {html.escape(machine_label)}",
        f"Detected at: {local_ts(ts)}",
        "Status: LIVE",
    ]
    if account_label:
        lines.append(f"Account: {html.escape(account_label)}")
    if after_gap:
        lines.append(f"<i>LIVE was first observed after a monitoring gap of {format_duration(gap_seconds)}; "
                     "the exact start time was not observed.</i>")
    if not screenshot_attached:
        lines.append("<i>Screenshot unavailable (screenshots disabled or no valid frame).</i>")
    if not rules_verified:
        lines.append("<i>Live-state rules are unverified (not yet calibrated on real Studio screenshots).</i>")
    return _finish(lines, ts)


def format_already_live(machine_label: str, ts: float, account_label: str = "", screenshot_attached: bool = True,
                        rules_verified: bool = True) -> dict:
    lines = [
        "<b>TIKTOK LIVE STUDIO IS ALREADY LIVE</b>",
        f"PC: {html.escape(machine_label)}",
        f"Observed at: {local_ts(ts)}",
        "Status: LIVE \u2014 monitoring started while the broadcast was already running "
        "(this is not a newly observed broadcast start).",
    ]
    if account_label:
        lines.append(f"Account: {html.escape(account_label)}")
    if not screenshot_attached:
        lines.append("<i>Screenshot unavailable.</i>")
    if not rules_verified:
        lines.append("<i>Live-state rules are unverified (not yet calibrated on real Studio screenshots).</i>")
    return _finish(lines, ts)


def format_health_alert(kind: str, reason: str, machine_label: str, ts: float, since: float, duration: float) -> dict:
    if kind == "degraded":
        lines = [
            "\u26A0\uFE0F <b>MONITOR HEALTH: DEGRADED</b>",
            f"PC: {html.escape(machine_label)}",
            f"Reason: {html.escape(reason) or '-'}",
            f"Since: {local_ts(since)} (persisting for {format_duration(duration)})",
            "Popup detection may be incomplete until capture recovers. Restriction alerts are not affected by this notice.",
        ]
    else:
        lines = [
            "\u2705 <b>MONITOR HEALTH: RECOVERED</b>",
            f"PC: {html.escape(machine_label)}",
            f"Previous problem: {html.escape(reason) or '-'}",
            f"Degraded from {local_ts(since)} for {format_duration(duration)}; healthy again at {local_ts(ts)}.",
        ]
    return _finish(lines, ts)


def format_status_alert(status: str, reason: str, machine_label: str, ts: float) -> str:
    return "\n".join([
        f"ℹ️ <b>{html.escape(SOURCE_LABEL)}</b> monitor status: <b>{html.escape(status)}</b>",
        f"<b>Reason:</b> {html.escape(reason) or '-'}",
        f"<b>Time:</b> {html.escape(_ts(ts))}",
        f"<b>Machine:</b> {html.escape(machine_label)}",
    ])
