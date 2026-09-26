"""Build Strategist: turns what's happening + what performs into Build Lab ideas."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import select

from xcp import config, db, llm, notify, showcase
from xcp.agents import context
from xcp.timeutil import today_ny, utcnow

log = logging.getLogger(__name__)


def _posts_block(days: int = 14) -> str:
    since = utcnow() - timedelta(days=days)
    with db.session() as s:
        rows = s.scalars(select(db.Post).where(db.Post.posted_at >= since).order_by(db.Post.posted_at.desc())
                         .limit(40)).all()
    lines = []
    for p in rows:
        m = p.latest_metrics or {}
        lines.append(f"[{p.pillar}/{p.kind}] impressions {m.get('impressions', '?')}, likes {m.get('likes', '?')}, "
                     f"reposts {m.get('reposts', '?')}: {p.text[:220]}")
    return "\n".join(lines) or "(no tracked posts yet)"


def _stories_block(days: int = 7) -> str:
    since = (today_ny() - timedelta(days=days)).isoformat()
    with db.session() as s:
        rows = s.scalars(select(db.Story).where(db.Story.slot_date >= since).order_by(db.Story.score.desc())
                         .limit(30)).all()
    return "\n".join(f"[{r.pillar}] ({r.score}) {r.title}: {r.summary[:200]}" for r in rows) or "(none)"


def _existing_block() -> str:
    with db.session() as s:
        rows = s.scalars(select(db.BuildIdea).order_by(db.BuildIdea.created_at.desc()).limit(60)).all()
    return "\n".join(f"- [{r.status}] {r.title} ({r.format})" for r in rows) or "(none yet)"


def _lineup_block() -> str:
    rows = showcase.lineup(14)
    return "\n".join(f"{r['date']:%a %Y-%m-%d} {r['slot']}: "
                     f"{(r['idea'].title + ' [' + r['idea'].status + ']') if r['idea'] else 'EMPTY'}"
                     for r in rows) or "(no showcase slots)"


def _mock_ideas(n: int, weekly: bool) -> dict:
    ideas = []
    for i in range(n):
        ideas.append({
            "title": f"[mock] STRC Par Keeper sim #{i + 1}", "hook": "Watch STRC get pulled back to $100.",
            "format": "gif_sim", "pillar": "digital_credit", "why_now": "mock", "signal_links": [],
            "concept": "Price wanders; monthly rate resets pull it to par.", "data_inputs": "yfinance STRC",
            "build_spec": "1. simulate 2. animate 3. export GIF", "effort": "S", "impact": 7, "novelty": 6,
            "timeliness": 5, "evergreen": True, "expires_in_days": 0,
            "launch_post": "STRC's rate reset works like a thermostat. Here's what that looks like:",
            "followups": ["mock followup 1", "mock followup 2"],
            "build_prompt": "Build a GIF simulation of STRC price mean-reverting to $100 par...", "series": ""})
    lineup = []
    if weekly:
        for r in showcase.lineup(14):
            if not r["idea"] and ideas:
                lineup.append({"date": r["date"].isoformat(), "slot": r["slot"], "idea_title": ideas[0]["title"]})
                break
    return {"ideas": ideas, "memo": "[mock] Weekly memo." if weekly else "", "lineup": lineup}


def generate(n: int = 3, weekly: bool = False, focus: str = "") -> dict:
    snap_text, snap_time, _ = context.snapshot_block()
    if weekly:
        mode = ("WEEKLY deep pass. Review the week: what performed, what didn't, which pillars are under target. "
                "Propose ideas for the next two weeks of showcase slots.")
        task = (f"Return {n} new ideas. memo: a short weekly review for the owner (what worked, what to try, the "
                f"mix vs 80/20, 5-8 bullets). lineup: for each EMPTY showcase slot in the next 2 weeks, suggest "
                f"an idea title, either from your new ideas or an existing inbox/shortlist idea.")
    else:
        mode = "DAILY light pass. React to today's stories and yesterday's performance."
        task = f"Return {n} new ideas. memo: an empty string. lineup: an empty array."
    if focus:
        mode += f"\nOwner's focus for this request: {focus}"
    prompt = llm.render_prompt("ideas", handle=context.handle(), mode=mode, posts=_posts_block(),
                               stories=_stories_block(), existing=_existing_block(), lineup=_lineup_block(),
                               snapshot=snap_text, snapshot_time=snap_time, task=task)
    return llm.run_json(prompt, "ideas", mock=lambda: _mock_ideas(n, weekly))


def save_ideas(ideas: list[dict], note: str = "") -> list[int]:
    ids = []
    with db.session() as s:
        for x in ideas:
            exp = None
            if x.get("expires_in_days"):
                exp = (today_ny() + timedelta(days=int(x["expires_in_days"]))).isoformat()
            row = db.BuildIdea(
                title=x["title"], hook=x["hook"], format=x["format"], pillar=x["pillar"],
                lane=config.pillar_lane(x["pillar"]), why_now=x["why_now"], signal_links=x.get("signal_links", []),
                concept=x["concept"], data_inputs=x["data_inputs"], build_spec=x["build_spec"], effort=x["effort"],
                impact=_clamp(x["impact"]), novelty=_clamp(x["novelty"]), timeliness=_clamp(x["timeliness"]),
                evergreen=bool(x["evergreen"]), expires_on=exp, launch_post=x["launch_post"],
                followups=x.get("followups", []), build_prompt=x["build_prompt"], series=x.get("series", ""),
                notes=note, source="ai")
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
    return ids


def _clamp(v) -> int:
    try:
        return max(1, min(10, int(v)))
    except (TypeError, ValueError):
        return 5


def apply_lineup(lineup: list[dict]) -> list[str]:
    applied = []
    with db.session() as s:
        for row in lineup:
            try:
                d = date.fromisoformat(row["date"])
            except ValueError:
                continue
            if showcase.assigned_idea(s, d.isoformat(), row["slot"]):
                continue
            idea = s.scalars(select(db.BuildIdea).where(db.BuildIdea.title == row["idea_title"],
                                                        db.BuildIdea.showcase_date.is_(None))).first()
            if idea:
                idea.showcase_date, idea.showcase_slot = d.isoformat(), row["slot"]
                idea.status = "shortlist" if idea.status == "inbox" else idea.status
                idea.notes = (idea.notes + "\nAuto-slotted by the weekly strategist.").strip()
                applied.append(f"{d:%a %b %d} {row['slot']}: {idea.title}")
        s.commit()
    return applied


def daily() -> dict:
    out = generate(n=3)
    ids = save_ideas(out.get("ideas", []))
    return {"ideas": len(ids)}


def weekly() -> dict:
    out = generate(n=6, weekly=True)
    ids = save_ideas(out.get("ideas", []), note="From the weekly review.")
    memo = out.get("memo", "")
    db.kv_set("weekly_memo", {"date": today_ny().isoformat(), "text": memo})
    applied = apply_lineup(out.get("lineup", []))
    lineup_text = "\n".join(
        f"{r['date']:%a %b %d} {r['label']}: " + (f"{r['idea'].title} [{r['idea'].status}]" if r["idea"] else "EMPTY")
        for r in showcase.lineup(7))
    notify.discord("📅 Weekly review + showcase lineup", memo[:3000],
                   [("This week's showcase lineup", lineup_text or "—"),
                    ("New ideas", f"{len(ids)} added to Build Lab"),
                    ("Auto-slotted", "\n".join(applied) or "—")])
    return {"ideas": len(ids), "lineup_applied": len(applied)}


def readiness_alert() -> str | None:
    """Warn the night before if tomorrow's showcase build isn't ready."""
    tomorrow = today_ny() + timedelta(days=1)
    msgs = []
    with db.session() as s:
        for slot in showcase.showcase_slots(tomorrow):
            idea = showcase.assigned_idea(s, tomorrow.isoformat(), slot) or showcase.pick_for_slot(s, tomorrow, slot)
            if idea is None:
                msgs.append(f"{slot}: nothing assigned or ready")
            elif idea.status not in showcase.READY:
                msgs.append(f"{slot}: '{idea.title}' is still [{idea.status}]")
    if msgs:
        text = "\n".join(msgs)
        notify.discord(f"⚠️ Tomorrow's showcase ({tomorrow:%a %b %d}) needs attention", text, color=0xE74C3C)
        return text
    return None
