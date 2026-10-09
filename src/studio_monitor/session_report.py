"""Session and broadcast reports: counts and durations computed from the
database (incident engine, activity events, reminders, detectors), rendered
once as Telegram HTML and once as plain text for memory. Nothing is inferred."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Optional

from .alerts import format_duration, headline, local_ts


def _utc_s(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def build_report(kind: str, *, device_id: str, device_name: str, owner_label: str, session_id: str, episode_id: str,
                 started_utc: str, ended_at: float, summary: dict, reminders_sent: int, offline_seconds: float,
                 stream_problems: dict, account: str, account_status: str, live_rules_verified: bool, note: str = "") -> dict:
    """``kind`` is ``broadcast_report`` (episode ended) or ``session_report`` (Studio closed)."""
    started = None
    try:
        started = datetime.fromisoformat(started_utc) if started_utc else None
    except ValueError:
        started = None
    duration = (ended_at - started.timestamp()) if started else 0.0
    by_cat = summary.get("by_category", {})
    open_count = sum(b.get("open", 0) for b in by_cat.values())
    rid = f"{'B' if kind == 'broadcast_report' else 'S'}-{episode_id or session_id}"
    title = "BROADCAST REPORT" if kind == "broadcast_report" else "SESSION REPORT"
    lines = [headline("\U0001F4CB", owner_label, title),
             f"{'Broadcast' if kind == 'broadcast_report' else 'Studio session'} {html.escape(episode_id or session_id or '-')}"
             + (f" lasted {format_duration(duration)}" if started else ""),
             f"Ended: {local_ts(ended_at)}"]
    if account:
        lines.append(f"TikTok account: {html.escape(account)} ({account_status.lower()})")
    lines.append(f"Incidents: {summary.get('incidents', 0)} ({open_count} still open)")
    for cat, b in by_cat.items():
        lines.append(f"  – {html.escape(cat)}: {b['count']} ({b['resolved']} resolved, {format_duration(b['total_seconds'])} total)")
    if kind == "session_report":
        lines.append(f"Go-live reminders sent: {reminders_sent}; offline time counted: {format_duration(offline_seconds)}")
    problems = {k: v for k, v in (stream_problems or {}).items() if v}
    if problems:
        lines.append("Stream-health episodes: " + ", ".join(f"{k.lower()} ×{v}" for k, v in problems.items()))
    if not live_rules_verified:
        lines.append("Note: live-state rules are unverified; broadcast timing is from seeded rules.")
    if note:
        lines.append(html.escape(note))
    lines.append(f"PC: {html.escape(device_name)}")
    text_html = "\n".join(lines)
    plain = (f"{title.title()} for {device_name} ({owner_label}). "
             f"{'Broadcast' if kind == 'broadcast_report' else 'Session'} {episode_id or session_id} "
             f"{'lasted ' + format_duration(duration) + ', ' if started else ''}ended {_utc_s(ended_at)}. "
             f"Incidents: {summary.get('incidents', 0)} ({open_count} open)"
             + (": " + "; ".join(f"{c} {b['count']}" for c, b in by_cat.items()) if by_cat else "") + ". "
             + (f"Account {account}. " if account else "")
             + (f"Stream problems: {', '.join(f'{k.lower()} x{v}' for k, v in problems.items())}. " if problems else "")
             + (f"Reminders sent {reminders_sent}. " if kind == "session_report" else ""))
    return {"report_id": rid, "kind": kind, "device_id": device_id, "device_name": device_name, "session_id": session_id,
            "episode_id": episode_id, "started_utc": started_utc, "ended_utc": _utc_s(ended_at), "duration_seconds": duration,
            "incident_count": summary.get("incidents", 0), "open_incidents": open_count, "by_category": by_cat,
            "reminders_sent": reminders_sent, "offline_seconds": offline_seconds, "stream_problems": problems,
            "account": account, "account_status": account_status, "text_html": text_html, "text_plain": plain}
