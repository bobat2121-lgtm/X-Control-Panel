"""Showcase slots: Mon pre-market, Wed midday, Fri after close post a Build Lab creation."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from xcp import config, db
from xcp.timeutil import dayname, upcoming_dates, utcnow

READY = ("ready", "shipped")


def showcase_slots(d: date) -> list[str]:
    return [x["slot"] for x in config.settings().get("showcase", []) if str(x.get("day", ""))[:3].lower() == dayname(d)]


def is_showcase(slot: str, d: date) -> bool:
    return slot in showcase_slots(d)


def assigned_idea(s, date_str: str, slot: str) -> db.BuildIdea | None:
    return s.scalars(select(db.BuildIdea).where(db.BuildIdea.showcase_date == date_str,
                                                db.BuildIdea.showcase_slot == slot)).first()


def already_showcased_ids(s) -> set[int]:
    rows = s.scalars(select(db.Draft.build_idea_id).where(db.Draft.kind == "showcase",
                                                          db.Draft.build_idea_id.is_not(None))).all()
    return {r for r in rows if r}


def pick_for_slot(s, d: date, slot: str) -> db.BuildIdea | None:
    idea = assigned_idea(s, d.isoformat(), slot)
    if idea:
        return idea
    used = already_showcased_ids(s)
    candidates = s.scalars(select(db.BuildIdea).where(db.BuildIdea.status.in_(READY),
                                                      db.BuildIdea.showcase_date.is_(None))
                           .order_by(db.BuildIdea.updated_at.desc())).all()
    return next((c for c in candidates if c.id not in used), None)


def existing_showcase_draft(s, date_str: str, slot: str) -> db.Draft | None:
    return s.scalars(select(db.Draft).where(db.Draft.kind == "showcase", db.Draft.slot == slot,
                                            db.Draft.slot_date == date_str)).first()


def lineup(n_days: int = 14) -> list[dict]:
    slots_cfg = config.settings().get("slots", {})
    out = []
    with db.session() as s:
        for d in upcoming_dates(n_days):
            for slot in showcase_slots(d):
                idea = assigned_idea(s, d.isoformat(), slot)
                out.append({"date": d, "slot": slot, "label": slots_cfg.get(slot, {}).get("label", slot),
                            "post_at": slots_cfg.get(slot, {}).get("post_at", ""), "idea": idea})
    return out


def default_showcase_text(idea: db.BuildIdea) -> str:
    text = idea.launch_post or f"{idea.hook or idea.title}"
    link = idea.shipped_url or idea.media_url
    if link and link not in text:
        text = f"{text}\n\n{link}"
    return text


def create_showcase_draft(s, idea: db.BuildIdea, slot: str, date_str: str,
                          variants: list[dict] | None = None) -> db.Draft:
    d = db.Draft(slot=slot, slot_date=date_str, kind="showcase", lane=idea.lane, pillar=idea.pillar,
                 tone="analytical", status="new", score=100.0, title=f"Showcase: {idea.title}",
                 build_idea_id=idea.id, chart_hint="none",
                 inspiration=[{"label": "Build", "url": idea.shipped_url or idea.media_url or "",
                               "author": "Build Lab", "text": idea.hook or idea.title}])
    s.add(d)
    s.flush()
    variants = variants or [{"label": "A", "style": "showcase", "parts": [default_showcase_text(idea)]}]
    for i, v in enumerate(variants):
        db.add_variant_version(s, d.id, v.get("label") or "ABC"[i], v["parts"], v.get("style", "showcase"), "ai")
    idea.showcase_date, idea.showcase_slot = date_str, slot
    idea.updated_at = utcnow()
    return d
