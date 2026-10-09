"""Compose Telegram notification text.

Every notification starts with a shared headline built by :func:`headline`:
``<icon> <label> — <TITLE>`` where the label is the operator-entered owner
label (e.g. "Roy’s Live"), HTML-escaped. Messages use Telegram HTML parse
mode; all operator/OCR text is escaped.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional

from . import SOURCE_LABEL
from .incidents import Incident
from .privacy import bounded_text

MANUAL_ATTENTION = "Manual attention required"

_INCIDENT_TITLES = {
    "verification_puzzle": ("\U0001F9E9", "VERIFICATION REQUIRED"),
    "account_suspension": ("\U0001F6D1", "ACCOUNT SUSPENSION"),
    "live_interruption": ("\U0001F4F4", "LIVE INTERRUPTED"),
    "restriction_notice": ("⚠️", "RESTRICTION DETECTED"),
    "content_warning": ("⚠️", "CONTENT WARNING"),
}


def headline(icon: str, label: str, title: str) -> str:
    """Shared notification headline: ``icon <b>label — TITLE</b>``."""
    return f"{icon} <b>{html.escape(label)} — {html.escape(title)}</b>"


def _ts(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts).astimezone()
    except (OSError, OverflowError, ValueError):  # Windows rejects timestamps near the epoch
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


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


# ---------------------------------------------------------------- restrictions / verification

def format_alert(incident: Incident, machine_label: str, max_text: int = 400,
                 screenshot_attached: bool = True, reason: str = "",
                 capture_method: Optional[str] = None, label: str = "", account: str = "") -> dict:
    """Return ``{"caption": html, "text": html}`` for a popup incident."""
    label = label or f"{machine_label}’s Live"
    icon, title = _INCIDENT_TITLES.get(incident.category, ("\U0001F6A8", "ALERT"))
    where = "separate dialog" if incident.is_dialog else "main window"
    if incident.window_title:
        where += f' "{html.escape(incident.window_title)}"'
    detected = html.escape(bounded_text(incident.text, max_text)) or "(no text)"
    ts = incident.last_alerted or incident.last_seen
    lines = [headline(icon, label, title)]
    if incident.manual_attention:
        lines.append(f"{MANUAL_ATTENTION}. Studio is waiting for a human to complete this step; "
                     "the monitor does not interact with Studio.")
    if account:
        lines.append(f"TikTok account: {html.escape(account)}")
    lines += [
        f"<b>Reason:</b> <i>{detected}</i>",
        f"<b>Time:</b> {local_ts(ts)}",
        f"<b>Source:</b> {html.escape(SOURCE_LABEL)} ({html.escape(incident.label)})",
        f"<b>Category:</b> {html.escape(incident.category)}",
        f"<b>Where:</b> {where}",
        f"<b>Machine:</b> {html.escape(machine_label)}",
        f"<b>Incident ID:</b> <code>{html.escape(incident.incident_id)}</code>",
    ]
    if reason:
        lines.append(f"<b>Note:</b> {html.escape(reason)}")
    if not screenshot_attached:
        lines.append("<i>Screenshot not attached (privacy setting).</i>")
    out = _finish(lines, ts)
    out.pop("created_at", None)
    return out


# ---------------------------------------------------------------- Studio session

def format_studio_opened(machine_label: str, ts: float, screenshot_attached: bool,
                         timeout_seconds: float = 0.0, label: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    lines = [
        headline("\U0001F7E2", label, "STUDIO OPENED"),
        f"{html.escape(SOURCE_LABEL)} is now running.",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
    ]
    if not screenshot_attached:
        lines.append(f"<i>Screenshot unavailable: no usable capture of the Studio window within "
                     f"{int(timeout_seconds)} s.</i>")
    return _finish(lines, ts)


def format_studio_already_running(machine_label: str, ts: float, screenshot_attached: bool, label: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    lines = [
        headline("\U0001F7E2", label, "STUDIO ALREADY RUNNING"),
        "Studio already running — monitoring started.",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
    ]
    if not screenshot_attached:
        lines.append("<i>Screenshot unavailable at monitor start.</i>")
    return _finish(lines, ts)


def format_studio_closed(machine_label: str, ts: float, screenshot_captured_at: Optional[float],
                         label: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    lines = [
        headline("⚫", label, "STUDIO CLOSED"),
        f"{html.escape(SOURCE_LABEL)} has closed.",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
    ]
    if screenshot_captured_at is not None:
        lines.append("Image: last available screenshot before closure.")
        lines.append(f"Screenshot captured: {local_ts(screenshot_captured_at)}")
    else:
        lines.append("<i>No screenshot available from before closure.</i>")
    return _finish(lines, ts)


# ---------------------------------------------------------------- reminders

def format_not_live_reminder(machine_label: str, ts: float, threshold_minutes: float, offline_seconds: float,
                             episode_id: str, sequence: int = 1, max_count: int = 1,
                             screenshot_attached: bool = True, rules_verified: bool = True, label: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    hours = threshold_minutes / 60.0
    period = f"{int(hours)} hour{'s' if int(hours) != 1 else ''}" if hours >= 1 and hours == int(hours) \
        else f"{int(threshold_minutes)} minutes"
    lines = [
        headline("⏰", label, "GO-LIVE REMINDER"),
        f"Studio has been confirmed not live for at least {period}.",
        "Open your broadcast setup and go live when ready.",
        f"PC: {html.escape(machine_label)}",
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


# ---------------------------------------------------------------- broadcast

def format_broadcast_started(machine_label: str, ts: float, account_label: str = "", after_gap: bool = False,
                             gap_seconds: float = 0.0, screenshot_attached: bool = True, rules_verified: bool = True,
                             label: str = "", account_line: str = "", account_note: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    lines = [
        headline("\U0001F534", label, "HAS GONE LIVE"),
        f"{html.escape(SOURCE_LABEL)} is broadcasting.",
    ]
    if account_line:
        lines.append(f"TikTok account: {html.escape(account_line)}")
    lines += [
        f"Detected at: {local_ts(ts)}",
        "Status: LIVE",
        f"PC: {html.escape(machine_label)}",
    ]
    if account_note:
        lines.append(f"<i>{html.escape(account_note)}</i>")
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
                        rules_verified: bool = True, label: str = "", account_line: str = "", account_note: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    lines = [
        headline("\U0001F534", label, "ALREADY LIVE"),
        f"{html.escape(SOURCE_LABEL)} was already broadcasting when monitoring started "
        "(this is not a newly observed broadcast start).",
    ]
    if account_line:
        lines.append(f"TikTok account: {html.escape(account_line)}")
    lines += [
        f"Observed at: {local_ts(ts)}",
        "Status: LIVE",
        f"PC: {html.escape(machine_label)}",
    ]
    if account_note:
        lines.append(f"<i>{html.escape(account_note)}</i>")
    if account_label:
        lines.append(f"Account: {html.escape(account_label)}")
    if not screenshot_attached:
        lines.append("<i>Screenshot unavailable.</i>")
    if not rules_verified:
        lines.append("<i>Live-state rules are unverified (not yet calibrated on real Studio screenshots).</i>")
    return _finish(lines, ts)


# ---------------------------------------------------------------- health

def format_health_alert(kind: str, reason: str, machine_label: str, ts: float, since: float, duration: float,
                        label: str = "") -> dict:
    label = label or f"{machine_label}’s Live"
    if kind == "degraded":
        lines = [
            headline("⚠️", label, "MONITOR DEGRADED"),
            f"Reason: {html.escape(reason) or '-'}",
            f"Since: {local_ts(since)} (persisting for {format_duration(duration)})",
            f"PC: {html.escape(machine_label)}",
            "Popup detection may be incomplete until capture recovers. Restriction alerts are not affected by this notice.",
        ]
    else:
        lines = [
            headline("✅", label, "MONITOR RECOVERED"),
            f"Previous problem: {html.escape(reason) or '-'}",
            f"Degraded from {local_ts(since)} for {format_duration(duration)}; healthy again at {local_ts(ts)}.",
            f"PC: {html.escape(machine_label)}",
        ]
    return _finish(lines, ts)


# ---------------------------------------------------------------- stream health (detectors)

STREAM_TITLES = {
    "RECONNECTING": ("📶", "CONNECTION PROBLEM"),
    "SOURCE_MISSING": ("🖥️", "SOURCE MISSING"),
    "BLACK_PREVIEW": ("⬛", "PREVIEW BLACK"),
    "FACE_ABSENT": ("👤", "PRESENTER NOT VISIBLE"),
    "FACE_MOTION_LOW": ("🧍", "PRESENTER VERY STILL"),
    "PREVIEW_FROZEN": ("🧊", "PREVIEW MAY BE FROZEN"),
    "AUDIO_SILENCE": ("🔇", "AUDIO METER SILENT"),
}


def format_stream_alert(condition: str, detail: str, machine_label: str, ts: float, since: float,
                        label: str = "", account: str = "", screenshot_attached: bool = False,
                        rules_verified: bool = True, episodes: int = 1) -> dict:
    """Cautious wording: states the evidence, never a verdict about the broadcast itself."""
    label = label or f"{machine_label}\u2019s Live"
    icon, title = STREAM_TITLES.get(condition, ("\u26A0\uFE0F", condition.replace("_", " ")))
    lines = [headline(icon, label, title), html.escape(detail)]
    if account:
        lines.append(f"TikTok account: {html.escape(account)}")
    lines += [f"Since: {local_ts(since)}", f"Detected at: {local_ts(ts)}", f"PC: {html.escape(machine_label)}"]
    if episodes > 1:
        lines.append(f"Occurrence {episodes} during this broadcast.")
    if condition in ("FACE_ABSENT", "FACE_MOTION_LOW", "PREVIEW_FROZEN"):
        lines.append("Evidence: configured presenter region of the Studio window only. No identity recognition is performed.")
    if condition == "AUDIO_SILENCE":
        lines.append("Evidence: Studio\u2019s on-screen audio meter, not system audio.")
    if condition in ("RECONNECTING", "SOURCE_MISSING") and not rules_verified:
        lines.append("Note: message wording rules are seeded and not yet verified against real Studio screenshots.")
    lines.append("Screenshot attached." if screenshot_attached else "No screenshot attached.")
    return _finish(lines, ts)


def format_stream_recovered(condition: str, detail: str, machine_label: str, ts: float, duration: float,
                            label: str = "") -> dict:
    label = label or f"{machine_label}\u2019s Live"
    _, title = STREAM_TITLES.get(condition, ("", condition.replace("_", " ")))
    lines = [headline("\u2705", label, f"{title} \u2014 CLEARED"), html.escape(detail),
             f"Lasted: {format_duration(duration)}", f"Time: {local_ts(ts)}", f"PC: {html.escape(machine_label)}"]
    return _finish(lines, ts)


def format_pc_health_alert(kind: str, condition: str, text: str, machine_label: str, ts: float, label: str = "",
                           sample: Optional[dict] = None) -> dict:
    label = label or f"{machine_label}\u2019s Live"
    title = condition.replace("_", " ")
    if kind == "problem":
        lines = [headline("\U0001F4BB", label, f"PC HEALTH: {title}"), html.escape(text)]
        if sample:
            lines.append(f"CPU {sample.get('cpu_percent', 0):.0f}% \u00b7 memory {sample.get('memory_percent', 0):.0f}% \u00b7 "
                         f"disk free {sample.get('disk_free_percent', 0):.0f}%"
                         + (f" \u00b7 upload {sample.get('upload_kbps'):.0f} kbps" if sample.get("upload_kbps") is not None else ""))
        lines.append("Measured on the PC by the monitor; it does not change what Studio is doing.")
    else:
        lines = [headline("\u2705", label, f"PC HEALTH OK: {title}"), html.escape(text)]
    lines += [f"Time: {local_ts(ts)}", f"PC: {html.escape(machine_label)}"]
    return _finish(lines, ts)


# ---------------------------------------------------------------- end-LIVE confirmation dialog

def format_end_requested(machine_label: str, ts: float, account_line: str, label: str = "", screenshot_attached: bool = True,
                         title: str = "End streaming?", body: str = "End LIVE? Share your LIVE for more viewers.",
                         buttons: str = "End now / Cancel") -> dict:
    label = label or f"{machine_label}\u2019s Live"
    lines = [headline("\U0001F7E0", label, "END-LIVE CONFIRMATION OPENED"),
             f"TikTok account: {html.escape(account_line or 'unavailable')}",
             f"Title: {html.escape(title)}",
             f"Message: {html.escape(body)}",
             f"Buttons: {html.escape(buttons)}",
             "The broadcast has not yet been confirmed ended.",
             f"Observed: {local_ts(ts)}",
             f"PC: {html.escape(machine_label)}"]
    if not screenshot_attached:
        lines.append("No screenshot attached.")
    return _finish(lines, ts)


def format_unknown_popup(machine_label: str, ts: float, title: str, body: str, buttons: list[str], label: str = "",
                         account_line: str = "", readable: bool = True, screenshot_attached: bool = True,
                         more: list[tuple[str, str, list[str]]] | None = None, suppressed: int = 0) -> dict:
    """``more``: further unreadable-category blocks seen on the same frame (title, body, buttons) -> one alert per frame;
    ``suppressed``: distinct unknown popups seen since the last review alert but held back by the review cooldown."""
    label = label or f"{machine_label}\u2019s Live"
    lines = [headline("\U0001F4AC", label, "NEW STUDIO POPUP \u2014 NEEDS REVIEW"),
             "A Studio dialog appeared that the monitor does not recognise. It is reported as seen, not interpreted."]
    if readable:
        lines += [f"Title: {html.escape(title or '(none)')}", f"Message: {html.escape(body or '(none)')}",
                  f"Buttons: {html.escape(' / '.join(buttons) or '(none read)')}"]
    else:
        lines.append("The popup text could not be read.")
    for i, (t2, b2, btn2) in enumerate(more or [], start=2):
        lines.append(f"Block {i}: {html.escape(t2 or '(none)')} \u2014 {html.escape((b2 or '')[:120])} [{html.escape(' / '.join(btn2) or 'no buttons')}]")
    if suppressed:
        lines.append(f"{suppressed} further unrecognised popup(s) since the last review alert were logged, not sent (review cooldown).")
    if account_line:
        lines.append(f"TikTok account: {html.escape(account_line)}")
    lines += [f"Observed: {local_ts(ts)}", f"PC: {html.escape(machine_label)}",
              "Screenshot attached." if screenshot_attached else "No screenshot attached."]
    return _finish(lines, ts)


def format_end_outcome(kind: str, machine_label: str, ts: float, incident_id: str, label: str = "", account_line: str = "",
                       reason: str = "") -> dict:
    label = label or f"{machine_label}\u2019s Live"
    if kind == "ended":
        lines = [headline("\u26AB", label, "LIVE HAS ENDED"),
                 "Studio is confirmed NOT LIVE after the end confirmation dialog."]
    elif kind == "continued":
        lines = [headline("\U0001F7E2", label, "END CONFIRMATION CLOSED \u2014 LIVE CONTINUES"),
                 "The \u201cEnd streaming?\u201d dialog is no longer visible and fresh evidence confirms the broadcast is still LIVE."]
    else:
        lines = [headline("\u2754", label, "END CONFIRMATION OUTCOME UNKNOWN"), html.escape(reason or "no further evidence")]
    if account_line:
        lines.append(f"TikTok account: {html.escape(account_line)}")
    lines += [f"Time: {local_ts(ts)}", f"PC: {html.escape(machine_label)}", f"Incident <code>{html.escape(incident_id)}</code>"]
    return _finish(lines, ts)


def format_status_alert(status: str, reason: str, machine_label: str, ts: float, label: str = "") -> str:
    label = label or f"{machine_label}’s Live"
    return "\n".join([
        headline("ℹ️", label, f"MONITOR {status}"),
        f"<b>Reason:</b> {html.escape(reason) or '-'}",
        f"<b>Time:</b> {local_ts(ts)}",
        f"<b>Machine:</b> {html.escape(machine_label)}",
    ])


# ---------------------------------------------------------------- tests

def format_test_notification(label: str, bot_name: str, destination: str, machine_label: str, ts: float,
                             screenshot_attached: bool) -> dict:
    lines = [
        headline("\U0001F9EA", label, "TEST NOTIFICATION"),
        "Test from Monitor Screen — no Studio event occurred.",
        f"Bot: {html.escape(bot_name)}",
        f"Destination: <code>{html.escape(destination)}</code>",
        f"PC: {html.escape(machine_label)}",
        f"Time: {local_ts(ts)}",
        "The attached image is synthetic. Nothing from the desktop was captured." if screenshot_attached
        else "Text-only test (screenshots disabled in privacy settings).",
    ]
    return _finish(lines, ts)
