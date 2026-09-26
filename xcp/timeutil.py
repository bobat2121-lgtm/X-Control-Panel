from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_ny() -> datetime:
    return datetime.now(NY)


def today_ny() -> date:
    return now_ny().date()


def dayname(d: date) -> str:
    return DAY_NAMES[d.weekday()]


def parse_hhmm(s: str) -> time:
    h, m = str(s).strip().split(":")
    return time(int(h), int(m))


def at_ny(d: date, hhmm: str) -> datetime:
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=NY)


def days_match(days, d: date) -> bool:
    if not days or days in ("all", "daily"):
        return True
    return dayname(d) in [str(x).lower()[:3] for x in days]


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything we store is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_hours(dt: datetime | None) -> float:
    if dt is None:
        return 999.0
    return max((utcnow() - aware(dt)).total_seconds() / 3600, 0.0)


def fmt_ago(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    h = age_hours(dt)
    if h < 1:
        return f"{int(h * 60)}m ago"
    if h < 48:
        return f"{h:.0f}h ago"
    return f"{h / 24:.0f}d ago"


def fmt_ny(dt: datetime | None, fmt: str = "%a %b %d %I:%M %p") -> str:
    if dt is None:
        return "—"
    return aware(dt).astimezone(NY).strftime(fmt)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return aware(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


def upcoming_dates(n_days: int, start: date | None = None) -> list[date]:
    start = start or today_ny()
    return [start + timedelta(days=i) for i in range(n_days)]
