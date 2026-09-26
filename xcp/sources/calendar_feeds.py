"""Calendar events the writer should know about, refreshed automatically (nightly, or Control Room → Calendar).

  FOMC rate decisions         federalreserve.gov FOMC calendar
  GDP and PCE releases        BEA release schedule (iCalendar)
  CPI, jobs, PPI              BLS release schedule (iCalendar). BLS only answers requests that carry a contact
                              email, so this source stays off until you allow your SEC contact to be used for it
  STRC record and pay dates   strategy.com STRC KPIs
  MSTR / ASST earnings        Nasdaq's date, marked "est." until confirmed
  Rate announcements, votes,  digital-exposure's curated data/calendar-events.json (read-only)
  confirmed earnings

Auto rows carry notes "auto:<source> ..."; rows you add yourself are never touched. A row that drops off an
official schedule is marked "superseded" (kept, hidden from the writer), never deleted.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from xcp import config, db
from xcp.config import env
from xcp.timeutil import NY, today_ny, utcnow

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; XControlPanel/1.0)"
FOMC_PAGE = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BEA_ICS = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
CURATED = "https://raw.githubusercontent.com/bobat2121-lgtm/digital-exposure/main/data/calendar-events.json"
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), 1)}
SUPERSEDED = "superseded"


def _get(url: str, ua: str = UA, accept: str = "*/*") -> str:
    r = httpx.get(url, headers={"User-Agent": ua, "Accept": accept}, timeout=25, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _event(d: date | str, title: str, *, time: str = "", pillar: str = "macro", importance: int = 2,
           detail: str = "") -> dict:
    return {"date": d if isinstance(d, str) else d.isoformat(), "time": time, "title": title[:200], "pillar": pillar,
            "importance": importance, "detail": detail}


# ------------------------------------------------------------------ iCalendar (BEA, BLS)

def parse_ics(text: str) -> list[tuple[datetime | date, str]]:
    """(start, summary) per VEVENT. Times come back in New York; all-day events as dates."""
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))
    out = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        summary = re.search(r"^SUMMARY[^:]*:(.*)$", block, re.M)
        start = re.search(r"^DTSTART([^:]*):(\d{8})(?:T(\d{4,6}))?(Z?)", block, re.M)
        if not (summary and start):
            continue
        params, day, hhmm, utc = start.groups()
        text_ = summary.group(1).replace("\\,", ",").replace("\\;", ";").strip()
        d = datetime.strptime(day, "%Y%m%d").date()
        if not hhmm:
            out.append((d, text_))
            continue
        t = datetime.strptime(day + hhmm[:4], "%Y%m%d%H%M")
        if utc:
            t = t.replace(tzinfo=timezone.utc).astimezone(NY)
        else:  # TZID=America/New_York, US-Eastern, or floating: all Washington release schedules are Eastern
            t = t.replace(tzinfo=NY)
        out.append((t, text_))
    return out


def _split(start) -> tuple[date, str]:
    return (start.date(), start.strftime("%H:%M")) if isinstance(start, datetime) else (start, "")


def bea() -> list[dict]:
    events = []
    for start, summary in parse_ics(_get(BEA_ICS)):
        d, t = _split(start)
        if summary.startswith("Personal Income and Outlays"):
            month = summary.split(",", 1)[-1].strip()
            events.append(_event(d, f"PCE inflation ({month})", time=t, importance=3,
                                 detail="BEA Personal Income and Outlays"))
        elif summary.startswith("GDP ("):
            m = re.match(r"GDP \(([^)]+)\).*?(\d)(?:st|nd|rd|th) Quarter (\d{4})", summary)
            label = f"GDP Q{m.group(2)} {m.group(3)} ({m.group(1).lower()})" if m else summary[:60]
            events.append(_event(d, label, time=t, importance=3 if m and "advance" in m.group(1).lower() else 2,
                                 detail="BEA"))
    return events


BLS_KEEP = (("Consumer Price Index", "CPI", 3), ("Employment Situation", "Jobs report", 3),
            ("Producer Price Index", "PPI", 2), ("Job Openings and Labor Turnover", "JOLTS", 1))


def bls(contact_ua: str) -> list[dict]:
    events = []
    for start, summary in parse_ics(_get(BLS_ICS, ua=contact_ua)):
        for needle, label, importance in BLS_KEEP:
            if summary.startswith(needle):
                month = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|"
                                  r"December)\s+\d{4}", summary)
                d, t = _split(start)
                events.append(_event(d, f"{label} ({month.group(0)})" if month else label, time=t,
                                     importance=importance, detail="BLS"))
                break
    return events


# ------------------------------------------------------------------ Fed

def parse_fomc(html: str) -> list[date]:
    """Decision days (the last day of each scheduled meeting) from the Fed's FOMC calendar page."""
    days = set()
    for block in re.split(r'<h4><a id="\d+">', html)[1:]:
        year = re.match(r"(\d{4}) FOMC Meetings", block)
        if not year:
            continue
        for month_text, day_text in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?'
                                               r'fomc-meeting__date[^>]*>([^<]+)<', block, re.S):
            if "notation" in day_text.lower() or "unscheduled" in day_text.lower():
                continue
            months = [MONTHS.get(p.strip().lower()[:3]) for p in month_text.split("/")]
            nums = [int(x) for x in re.findall(r"\d+", day_text)]
            if not nums or not months[0]:
                continue
            month = months[-1] if len(nums) > 1 and nums[-1] < nums[0] and months[-1] else months[0]
            days.add(date(int(year.group(1)), month, nums[-1]))
    return sorted(days)


def fomc() -> list[dict]:
    days = parse_fomc(_get(FOMC_PAGE, accept="text/html"))
    if not days:
        raise ValueError("FOMC page parsed to no meetings (layout changed?)")
    return [_event(d, "FOMC rate decision", time="14:00", importance=3, detail="federalreserve.gov") for d in days]


# ------------------------------------------------------------------ digital credit

def strategy_dates() -> list[dict]:
    row = httpx.get("https://api.strategy.com/btc/strcKpiData", headers={"User-Agent": UA}, timeout=20).json()[0]
    if row.get("company") != "STRC":
        raise ValueError(f"expected STRC, got {row.get('company')}")
    today = today_ny().isoformat()
    events, seen = [], set()
    rows = list(row.get("dividendHistory") or []) + [{"recordDate": row.get("nextRecordDate"),
                                                     "payDate": row.get("nextPayoutDate"), "rate": row.get("currentDividend")}]
    for r in rows:
        for key, title in (("recordDate", "STRC record date"), ("payDate", "STRC dividend paid")):
            d = str(r.get(key) or "")[:10]
            if d >= today and (d, title) not in seen:
                seen.add((d, title))
                rate = f" at {r['rate']}%/yr" if r.get("rate") else ""
                events.append(_event(d, title, pillar="digital_credit", importance=1, detail=f"strategy.com{rate}"))
    return events


def earnings() -> list[dict]:
    events = []
    for ticker in ("MSTR", "ASST"):
        data = httpx.get(f"https://api.nasdaq.com/api/analyst/{ticker}/earnings-date",
                         headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20).json()
        text = ((data or {}).get("data") or {}).get("reportText") or ""
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
        if m:
            est = "estimated" in text.lower()
            d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            events.append(_event(d, f"{ticker} earnings{' (est.)' if est else ''}", pillar="digital_credit",
                                 importance=3, detail="Nasdaq / Zacks estimate" if est else "Nasdaq"))
    return events


def curated() -> list[dict]:
    data = httpx.get(CURATED, headers={"User-Agent": UA}, timeout=20).json()
    events = []
    for e in data.get("events") or []:
        if not e.get("date") or not e.get("label"):
            continue
        label = e["label"]
        if not e.get("confirmed") and "est" not in label.lower():
            label += " (est.)"
        imp = {"earnings": 3, "rate": 2, "corporate": 2}.get(e.get("kind"), 1)
        events.append(_event(e["date"], label, pillar="digital_credit", importance=imp,
                             detail=f"digital-exposure: {str(e.get('source', ''))[:200]}"))
        events[-1]["kind"], events[-1]["ticker"] = e.get("kind"), e.get("ticker")
    return events


# ------------------------------------------------------------------ sync

def sources() -> dict:
    cfg = config.settings().get("calendar", {})
    out = {"fed": fomc, "bea": bea, "strategy": strategy_dates, "nasdaq": earnings, "curated": curated}
    if cfg.get("bls_contact") and env("SEC_USER_AGENT"):
        out["bls"] = lambda: bls(env("SEC_USER_AGENT"))
    return out


def sync() -> dict:
    cfg = config.settings().get("calendar", {})
    if not cfg.get("auto", True):
        return {"skipped": "automatic calendar is off"}
    today = today_ny()
    horizon = (today + timedelta(days=int(cfg.get("days_ahead", 60)))).isoformat()
    stats: dict = {"added": 0, "updated": 0, "superseded": 0, "by_source": {}, "errors": []}
    fetched: dict[str, list[dict]] = {}
    for name, fn in sources().items():
        try:
            fetched[name] = [e for e in fn() if today.isoformat() <= e["date"] <= horizon]
        except Exception as e:  # each source stands alone
            stats["errors"].append(f"{name}: {type(e).__name__}: {e}"[:240])
    confirmed = {e.get("ticker") for e in fetched.get("curated", []) if e.get("kind") == "earnings"}
    if "nasdaq" in fetched:  # a confirmed curated date replaces Nasdaq's estimate
        fetched["nasdaq"] = [e for e in fetched["nasdaq"] if e["title"].split()[0] not in confirmed]
    with db.session() as s:
        existing = list(s.scalars(select(db.CalendarEvent).where(db.CalendarEvent.date >= today.isoformat(),
                                                                 db.CalendarEvent.notes.like("auto:%"))).all())
        for name, events in fetched.items():
            mine = {(r.date, r.title): r for r in existing if (r.notes or "").startswith(f"auto:{name}")}
            seen = set()
            for ev in events:
                key = (ev["date"], ev["title"])
                seen.add(key)
                note = f"auto:{name} · {ev['detail']}".strip()
                row = mine.get(key)
                if row is None:
                    s.add(db.CalendarEvent(date=ev["date"], time=ev["time"], title=ev["title"], pillar=ev["pillar"],
                                           importance=ev["importance"], notes=note))
                    stats["added"] += 1
                elif (row.time, row.importance, row.notes) != (ev["time"], ev["importance"], note):
                    row.time, row.importance, row.pillar, row.notes = ev["time"], ev["importance"], ev["pillar"], note
                    stats["updated"] += 1
            if name != "strategy":  # STRC's list only shows the next few dates, so absence isn't a change
                for key, row in mine.items():
                    if key not in seen and row.date <= horizon and SUPERSEDED not in (row.notes or ""):
                        row.notes = f"{row.notes} · {SUPERSEDED} (no longer on the schedule)"
                        stats["superseded"] += 1
            stats["by_source"][name] = len(events)
        s.commit()
    if not cfg.get("bls_contact"):
        stats["note"] = "CPI and jobs dates are off: BLS needs a contact email (Control Room → Calendar)"
    db.kv_set("calendar:last_sync", {**stats, "at": utcnow().isoformat()})
    return stats
