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


# ------------------------------------------------------------------ market and SEC calendars

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (0=Mon) of a month; n=-1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def federal_holidays(year: int) -> set[date]:
    """US federal holidays as observed (EDGAR is closed on these)."""
    days = {_observed(date(year, 1, 1)), _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
            _nth_weekday(year, 5, 0, -1), _observed(date(year, 6, 19)), _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2), _observed(date(year, 11, 11)),
            _nth_weekday(year, 11, 3, 4), _observed(date(year, 12, 25))}
    nxt = _observed(date(year + 1, 1, 1))  # Jan 1 on a Saturday is observed on Dec 31
    if nxt.year == year:
        days.add(nxt)
    return days


def nyse_holidays(year: int) -> set[date]:
    days = {_nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), _easter(year) - timedelta(days=2),
            _nth_weekday(year, 5, 0, -1), _observed(date(year, 6, 19)), _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 11, 3, 4), _observed(date(year, 12, 25))}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # the NYSE does not close Dec 31 for a Saturday New Year
        days.add(_observed(new_year))
    return days


def edgar_open(d: date) -> bool:
    return d.weekday() < 5 and d not in federal_holidays(d.year)


def nyse_open(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def last_nyse_session(d: date) -> date:
    """d itself if the NYSE trades that day, else the session before it."""
    while not nyse_open(d):
        d -= timedelta(days=1)
    return d


def weekly_release_date(d: date) -> date:
    """The day this week's Monday 8-Ks are due: Monday, or Tuesday after an EDGAR Monday holiday."""
    monday = d - timedelta(days=d.weekday())
    return monday if edgar_open(monday) else monday + timedelta(days=1)
