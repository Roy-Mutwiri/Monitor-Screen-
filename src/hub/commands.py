"""Hub-side Telegram commands: fleet status, per-device screenshots via a
predefined remote operation, ack/snooze of hub incidents, session report.
One poller per route with ``commands_enabled``; the bot token comes from the
hub environment."""
from __future__ import annotations

import html
import logging
import time
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio_monitor.commands import CommandRouter, UpdatePoller
from studio_monitor.config import TelegramConfig
from studio_monitor.telegram import TelegramClient

from .db import Device, EventRow, Route
from .services import HubError, HubService, parse_iso

log = logging.getLogger("hub.commands")


class HubCommandBackend:
    def __init__(self, session_factory: sessionmaker[Session], workspace_id: str, clock: Callable[[], float] | None = None,
                 evidence_dir: str = "") -> None:
        self.sf = session_factory
        self.workspace_id = workspace_id
        self.clock = clock
        self.evidence_dir = evidence_dir
        self.memory = None

    def _svc(self, s: Session) -> HubService:
        return HubService(s, self.clock)

    def status(self) -> str:
        with self.sf() as s:
            svc = self._svc(s)
            devs = svc.devices(self.workspace_id)
            lines = [f"<b>Fleet status</b> — {len(devs)} device(s)"]
            for d in devs:
                st = svc.device_status(d)
                ls = (d.last_status or {})
                extra = f" · {ls.get('live_state')}" if ls.get("live_state") else ""
                acct = f" · {html.escape(d.observed_account)}" if d.observed_account else ""
                lines.append(f"• <b>{html.escape(d.name or d.id[:8])}</b> ({html.escape(d.owner_label or '-')}): {st}{extra}{acct}"
                             f"{' · last heartbeat ' + d.last_heartbeat_utc if d.last_heartbeat_utc else ''}")
            incs = svc.open_incidents(self.workspace_id)
            lines.append(f"Open incidents: {len(incs)}")
            for i in incs[:8]:
                lines.append(f"  – [{i.severity}] <code>{html.escape(i.incident_id)}</code> {html.escape(i.summary[:80])}")
            return "\n".join(lines)

    def screenshot(self, device_name: str = "") -> tuple[Optional[str], str]:
        """Queue the predefined ``screenshot`` operation for a device (executed by the agent on its next heartbeat)
        and return the most recent stored evidence meanwhile."""
        with self.sf() as s:
            svc = self._svc(s)
            devs = svc.devices(self.workspace_id)
            if not devs:
                return None, "No devices enrolled."
            target = None
            if device_name:
                target = next((d for d in devs if device_name.lower() in (d.name or "").lower() or d.id.startswith(device_name)), None)
                if target is None:
                    return None, f"No device matches '{html.escape(device_name)}'."
            elif len(devs) == 1:
                target = devs[0]
            else:
                return None, "Several devices: use /screenshot DEVICE_NAME.\n" + "\n".join(f"• {html.escape(d.name or d.id[:8])}" for d in devs)
            cmds = list(target.pending_commands or [])
            cmds.append({"op": "screenshot", "requested_utc": svc.now(), "requested_by": "telegram"})
            target.pending_commands = cmds[-5:]
            svc.audit("telegram", "command.screenshot", target.id)
            s.commit()
            last = s.scalars(select(EventRow).where(EventRow.device_id == target.id, EventRow.evidence_stored_path != "")
                             .order_by(EventRow.received_utc.desc()).limit(1)).first()
            note = (f"Screenshot requested from <b>{html.escape(target.name or target.id[:8])}</b>; it arrives with the next heartbeat "
                    f"(≤ {15} s while the agent is online).")
            if last is not None:
                return last.evidence_stored_path, note + f"\nLatest stored evidence ({last.observed_utc}, {html.escape(last.type)}):"
            return None, note

    def sessions(self, limit: int) -> str:
        with self.sf() as s:
            svc = self._svc(s)
            rows = svc.recent_events(None, 200, ["STUDIO_OPENED", "STUDIO_ALREADY_RUNNING", "STUDIO_CLOSED", "BROADCAST_STARTED",
                                                 "BROADCAST_ALREADY_LIVE", "BROADCAST_ENDED", "DEVICE_UNREACHABLE", "DEVICE_REACHABLE"])
            names = {d.id: (d.name or d.id[:8]) for d in svc.devices(self.workspace_id)}
            lines = ["<b>Recent sessions</b>"]
            for e in rows[:limit * 2]:
                lines.append(f"{e.observed_utc[11:16]} {html.escape(names.get(e.device_id, e.device_id[:8]))}: {html.escape(e.type)}")
            return "\n".join(lines) if len(lines) > 1 else "No session events yet."

    def ack(self, incident_id: str, actor: str) -> str:
        with self.sf() as s:
            try:
                inc = self._svc(s).ack(incident_id, actor)
                s.commit()
            except HubError as exc:
                return f"{html.escape(str(exc))}: <code>{html.escape(incident_id)}</code>"
            return f"Acknowledged <code>{html.escape(inc.incident_id)}</code> by {html.escape(actor)}. Reminders paused; the fault stays open until it clears."

    def snooze(self, incident_id: str, minutes: int, actor: str) -> str:
        with self.sf() as s:
            try:
                inc = self._svc(s).snooze(incident_id, minutes * 60, actor)
                s.commit()
            except HubError as exc:
                return f"{html.escape(str(exc))}: <code>{html.escape(incident_id)}</code>"
            return f"Snoozed <code>{html.escape(inc.incident_id)}</code> until {inc.snoozed_until_utc} (by {html.escape(actor)})."

    def report(self) -> str:
        with self.sf() as s:
            svc = self._svc(s)
            c = svc.counts()
            devs = svc.devices(self.workspace_id)
            live = sum(1 for d in devs if svc.device_status(d) == "LIVE")
            unreachable = sum(1 for d in devs if svc.device_status(d) == "UNREACHABLE")
            text = (f"<b>Fleet report</b>\nDevices: {len(devs)} (LIVE {live}, unreachable {unreachable})\n"
                    f"Open incidents: {c['open_incidents']}\nEvents stored: {c['events']}\nPending Telegram deliveries: {c['pending_deliveries']}")
            incs = svc.open_incidents(self.workspace_id)
            if self.memory is not None and getattr(self.memory, "configured", False):
                query = "; ".join(i.summary[:120] for i in incs[:3]) or "studio session report"
                block = self.memory.similar_block(self.workspace_id, query)
                if block:
                    text += "\n\n" + block
            return text


class HubCommandPollers:
    """One UpdatePoller per command-enabled route; polled from the background loop."""

    def __init__(self, session_factory: sessionmaker[Session], env: dict, api_base: str, transport=None,
                 clock: Callable[[], float] = time.time, evidence_dir: str = "", on_event: Optional[Callable[[str], None]] = None) -> None:
        self.sf, self.env, self.api_base, self.transport, self.clock = session_factory, env, api_base, transport, clock
        self.evidence_dir = evidence_dir
        self.on_event = on_event or (lambda m: log.info(m))
        self._pollers: dict[int, UpdatePoller] = {}
        self._state: dict[str, object] = {}
        self.memory = None

    def _state_get(self, key, default=None):
        return self._state.get(key, default)

    def _state_set(self, key, value) -> None:
        self._state[key] = value

    def refresh(self) -> None:
        with self.sf() as s:
            routes = list(s.scalars(select(Route).where(Route.enabled.is_(True), Route.commands_enabled.is_(True))))
        seen = set()
        for r in routes:
            seen.add(r.id)
            if r.id in self._pollers:
                continue
            token = self.env.get(r.token_env, "")
            if not token:
                continue
            client = TelegramClient(TelegramConfig(api_base=self.api_base, timeout_seconds=35.0), token, r.chat_id,
                                    int(r.thread_id) if r.thread_id else None, transport=self.transport)
            backend = HubCommandBackend(self.sf, r.workspace_id, self.clock, self.evidence_dir)
            backend.memory = self.memory
            router = CommandRouter(backend, {r.chat_id}, on_audit=self.on_event, clock=self.clock, actor_prefix="telegram")
            self._pollers[r.id] = UpdatePoller(client, router, self._state_get, self._state_set, f"route-{r.id}", "hub",
                                               on_event=self.on_event, clock=self.clock, long_poll_seconds=0)
        for rid in list(self._pollers):
            if rid not in seen:
                self._pollers.pop(rid)

    def poll_once(self) -> int:
        self.refresh()
        return sum(p.poll_once(timeout=0) for p in self._pollers.values())
