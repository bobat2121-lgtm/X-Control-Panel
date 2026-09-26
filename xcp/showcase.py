"""Showcase slots: Mon / Wed / Fri post the Digital Credit Report panels, each audited before it's drafted.

Monday = The Accretion Ledger (after both 8-Ks; Tuesday after an EDGAR Monday holiday),
Wednesday = The Coupon Sheet, Friday = The Closing Mark (after the 4:00 pm close).
The watcher (xcp/agents/showcase_watch.py) does the waiting, rendering and auditing.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from xcp import config, db
from xcp.showcase_gate import values_of
from xcp.sources import digital_exposure as de
from xcp.timeutil import dayname, upcoming_dates, utcnow, weekly_release_date


def panels() -> dict[str, dict]:
    return de.cfg().get("panels") or {}


def panel_for(d: date) -> str | None:
    """Which panel is showcased on this date, if any."""
    for name, spec in panels().items():
        day = str(spec.get("day", ""))[:3].lower()
        if name == "monday" and day == "mon":
            if d == weekly_release_date(d):
                return name
        elif day == dayname(d):
            return name
    return None


def showcase_slots(d: date) -> list[str]:
    p = panel_for(d)
    return [panels()[p].get("slot", "")] if p else []


def is_showcase(slot: str, d: date) -> bool:
    return slot in showcase_slots(d)


def title(panel: str) -> str:
    return panels().get(panel, {}).get("title", panel.title())


def run_for(s, date_str: str, panel: str) -> db.ShowcaseRun | None:
    return s.scalars(select(db.ShowcaseRun).where(db.ShowcaseRun.run_date == date_str, db.ShowcaseRun.panel == panel)
                     .order_by(db.ShowcaseRun.id.desc())).first()


def run_for_draft(s, draft_id: int) -> db.ShowcaseRun | None:
    return s.scalars(select(db.ShowcaseRun).where(db.ShowcaseRun.draft_id == draft_id)).first()


def lineup(n_days: int = 14) -> list[dict]:
    slots_cfg = config.settings().get("slots", {})
    out = []
    with db.session() as s:
        for d in upcoming_dates(n_days):
            p = panel_for(d)
            if not p:
                continue
            spec = panels()[p]
            slot = spec.get("slot", "")
            out.append({"date": d, "panel": p, "title": spec.get("title", p), "slot": slot,
                        "label": slots_cfg.get(slot, {}).get("label", slot), "post": spec.get("post", ""),
                        "window": f"{spec.get('start', '')}–{spec.get('deadline', '')}",
                        "run": run_for(s, d.isoformat(), p)})
    return out


def existing_showcase_draft(s, date_str: str, slot: str) -> db.Draft | None:
    return s.scalars(select(db.Draft).where(db.Draft.kind == "showcase", db.Draft.slot == slot,
                                            db.Draft.slot_date == date_str)).first()


# ------------------------------------------------------------------ the post

def _cash(ticker: str) -> str:
    return f"${ticker}"


def default_caption(panel: str, values: dict[str, str], filings: dict | None = None) -> str:
    """A fact-only caption built from the image's own figures (the AI adds voicier options on top)."""
    url = de.report_url(panel)
    v = values.get
    if panel == "monday":
        week = ""
        f = (filings or {}).get("MSTR") or {}
        if f.get("period_start") and f.get("balance_date"):
            a, b = date.fromisoformat(f["period_start"]), date.fromisoformat(f["balance_date"])
            week = f" · 8-K week {a:%b} {a.day}–" + (f"{b.day}" if a.month == b.month else f"{b:%b} {b.day}")
        lines = [f"The Accretion Ledger{week}", ""]
        for t in ("MSTR", "ASST"):
            bought, held, nav = v(f"{t} bitcoin bought"), v(f"{t} total BTC held"), v(f"{t} price / basic NAV")
            if bought and held:
                sign = "" if bought.strip().startswith(("+", "-", "−")) else "+"
                lines.append(f"{_cash(t)} {sign}{bought} → {held} held" + (f" · {nav} NAV" if nav else ""))
        lines += ["", f"Full ledger: {url}"]
        return "\n".join(lines)
    if panel == "wednesday":
        lines = ["The Coupon Sheet", ""]
        for t in ("STRC", "SATA"):
            eff, sp = v(f"{t} effective yield"), v(f"{t} spread over 3M bill")
            if eff:
                lines.append(f"{_cash(t)} {eff} effective" + (f" · {sp} over the 3M bill" if sp else ""))
        lines += ["", f"Full sheet: {url}"]
        return "\n".join(lines)
    if panel == "friday":
        week = v("Week ended") or ""
        wk = date.fromisoformat(week) if week else None
        lines = ["The Closing Mark" + (f" · week ended {wk:%b} {wk.day}" if wk else ""), ""]
        if v("BTC Friday 4 pm mark"):
            lines.append(f"$BTC {v('BTC Friday 4 pm mark')} at the 4 pm mark"
                         + (f" ({v('BTC weekly change')} wk)" if v("BTC weekly change") else ""))
        states = [val.rsplit("(", 1)[-1].rstrip(")") for k, val in values.items() if k.startswith("Checklist ·")]
        if states:
            tally = {s_: states.count(s_) for s_ in ("BULL", "NEUTRAL", "BEAR")}
            lines.append(f"Cycle checklist: {tally['BULL']} bull · {tally['NEUTRAL']} neutral · {tally['BEAR']} bear")
        lines += ["", f"Full mark: {url}"]
        return "\n".join(lines)
    return url


def create_draft(s, run: db.ShowcaseRun) -> db.Draft:
    """The Feed card for a ready panel: the image plus a fact-only caption (AI options arrive after)."""
    existing = existing_showcase_draft(s, run.run_date, run.slot)
    if existing:
        run.draft_id = existing.id
        return existing
    values = values_of({"panels": {run.panel: run.audit}}, run.panel)
    filings = run.filings or {}
    what = {"monday": "Both weekly 8-Ks are in the image.", "wednesday": "Coupon Sheet with today's rates.",
            "friday": "Closing Mark at the Friday 4:00 pm close."}.get(run.panel, "")
    d = db.Draft(slot=run.slot, slot_date=run.run_date, kind="showcase", lane="btc", pillar="digital_credit",
                 tone="analytical", status="new", score=100.0, title=f"Showcase: {run.title}",
                 chart_hint="none", editor_flags=[f"audit: {w['detail']}" for w in (run.warnings or [])][:8],
                 numbers=[{"label": k, "value": val, "source": "Digital Credit Report audit"}
                          for k, val in list(values.items())[:40]],
                 inspiration=[{"label": "report", "url": de.report_url(run.panel), "author": "Digital Credit Report",
                               "text": what + " " + " · ".join(
                                   f"{f.get('ticker')} 8-K {f.get('accession')} (balance {f.get('balance_date')})"
                                   for f in filings.values())}])
    s.add(d)
    s.flush()
    db.add_variant_version(s, d.id, "A", [default_caption(run.panel, values, filings)], "showcase", "tool:showcase")
    run.draft_id = d.id
    run.updated_at = utcnow()
    return d
