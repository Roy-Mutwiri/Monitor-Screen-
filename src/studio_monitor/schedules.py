"""Streaming schedules (per-device IANA timezone, weekdays, start/end, grace,
exceptions) and the missed-start / reminder-window logic.

Windows may cross midnight ("overnight"); DST changes are handled by doing
all arithmetic in the device's zone with :mod:`zoneinfo`. UNKNOWN broadcast
state never counts as a missed start.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class Schedule:
    enabled: bool = False
    timezone: str = "UTC"                # IANA zone, e.g. Africa/Nairobi
    weekdays: list[str] = field(default_factory=lambda: list(WEEKDAYS))
    start: str = "20:00"                 # HH:MM local
    end: str = "23:00"                   # HH:MM local; end <= start means overnight
    grace_minutes: int = 15
    exceptions: list[str] = field(default_factory=list)   # ISO dates (local) with no scheduled window

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        s = cls(**{k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__})
        s.weekdays = [w for w in s.weekdays if w in WEEKDAYS]
        return s

    def validate(self) -> list[str]:
        errs = []
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            errs.append(f"unknown timezone {self.timezone!r}")
        for label, value in (("start", self.start), ("end", self.end)):
            try:
                _parse_hm(value)
            except ValueError:
                errs.append(f"{label} must be HH:MM")
        if not self.weekdays:
            errs.append("at least one weekday is required")
        for d in self.exceptions:
            try:
                date.fromisoformat(d)
            except ValueError:
                errs.append(f"exception {d!r} must be an ISO date")
        return errs

    # ------------------------------------------------------------------
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def window_at(self, now_utc: datetime) -> Optional[tuple[datetime, datetime]]:
        """The scheduled window (UTC datetimes) containing ``now_utc``, or None.

        Overnight windows (end <= start) belong to the day they start on."""
        if not self.enabled:
            return None
        local = now_utc.astimezone(self.zone())
        for day in (local.date() - timedelta(days=1), local.date()):
            win = self._window_for_day(day)
            if win and win[0] <= now_utc < win[1]:
                return win
        return None

    def _window_for_day(self, day: date) -> Optional[tuple[datetime, datetime]]:
        if WEEKDAYS[day.weekday()] not in self.weekdays or day.isoformat() in self.exceptions:
            return None
        tz = self.zone()
        sh, sm = _parse_hm(self.start)
        eh, em = _parse_hm(self.end)
        start = datetime.combine(day, dtime(sh, sm), tzinfo=tz)
        end_day = day if (eh, em) > (sh, sm) else day + timedelta(days=1)
        end = datetime.combine(end_day, dtime(eh, em), tzinfo=tz)
        # normalise through UTC so DST gaps/overlaps resolve consistently
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def next_window(self, now_utc: datetime, days_ahead: int = 14) -> Optional[tuple[datetime, datetime]]:
        if not self.enabled:
            return None
        local = now_utc.astimezone(self.zone())
        for i in range(0, days_ahead + 1):
            win = self._window_for_day(local.date() + timedelta(days=i))
            if win and win[1] > now_utc:
                return win
        return None

    def missed_start(self, now_utc: datetime, confirmed_state: str) -> bool:
        """True when inside a window, past start + grace, and the broadcast is
        *confirmed* NOT_LIVE. UNKNOWN or LIVE never count."""
        win = self.window_at(now_utc)
        if win is None or confirmed_state != "NOT_LIVE":
            return False
        return now_utc >= win[0] + timedelta(minutes=self.grace_minutes)

    def reminders_allowed(self, now_utc: datetime) -> bool:
        """Ordinary go-live reminders only inside scheduled hours (always when disabled)."""
        return (not self.enabled) or self.window_at(now_utc) is not None

    def describe(self) -> str:
        if not self.enabled:
            return "no schedule"
        days = ",".join(self.weekdays)
        return f"{days} {self.start}-{self.end} {self.timezone} (grace {self.grace_minutes} min)"


def _parse_hm(value: str) -> tuple[int, int]:
    h, m = value.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(value)
    return h, m


@dataclass
class MaintenanceWindow:
    """Operator-declared break/maintenance with an explicit end."""
    until_utc: str = ""
    categories: list[str] = field(default_factory=list)
    reason: str = ""

    def active(self, now_utc: datetime) -> bool:
        if not self.until_utc:
            return False
        try:
            return datetime.fromisoformat(self.until_utc) > now_utc
        except ValueError:
            return False

    def remaining(self, now_utc: datetime) -> timedelta:
        if not self.active(now_utc):
            return timedelta(0)
        return datetime.fromisoformat(self.until_utc) - now_utc
