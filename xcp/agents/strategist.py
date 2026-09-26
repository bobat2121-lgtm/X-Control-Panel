"""Build Strategist: turns what's happening + what performs into Build Lab ideas."""
from __future__ import annotations

import logging
from datetime import timedelta

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
    head = ("The Mon/Wed/Fri showcase posts are fixed: the owner's Digital Credit Report panels (Mon The Accretion "
            "Ledger, Wed The Coupon Sheet, Fri The Closing Mark). Build Lab ideas are extra creations for regular "
            "slots, or upgrades to those three panels.")
    return "\n".join([head] + [f"{r['date']:%a %Y-%m-%d}: {r['title']}" for r in showcase.lineup(14)])


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
    return {"ideas": ideas, "memo": "[mock] Weekly memo." if weekly else "", "lineup": []}


def generate(n: int = 3, weekly: bool = False, focus: str = "") -> dict:
    snap_text, snap_time, _ = context.snapshot_block()
    if weekly:
        mode = ("WEEKLY deep pass. Review the week: what performed, what didn't, which pillars are under target. "
                "Propose ideas worth building next, including upgrades to the three Digital Credit Report panels.")
        task = (f"Return {n} new ideas. memo: a short weekly review for the owner (what worked, what to try, the "
                f"mix vs 80/20, 5-8 bullets). lineup: an empty array (the showcase slots are fixed).")
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


def daily() -> dict:
    out = generate(n=3)
    ids = save_ideas(out.get("ideas", []))
    return {"ideas": len(ids)}


def weekly() -> dict:
    out = generate(n=6, weekly=True)
    ids = save_ideas(out.get("ideas", []), note="From the weekly review.")
    memo = out.get("memo", "")
    db.kv_set("weekly_memo", {"date": today_ny().isoformat(), "text": memo})
    lineup_text = "\n".join(f"{r['date']:%a %b %d} {r['label']}: {r['title']} (post {r['post']})"
                            for r in showcase.lineup(7))
    fields = [("This week's showcase lineup", lineup_text or "—"), ("New ideas", f"{len(ids)} added to Build Lab")]
    pre = db.kv_get("showcase:preflight") or {}
    if pre.get("at"):
        fields.append(("Last preflight", f"digital-exposure {str(pre.get('commit', ''))[:7]}: " + " · ".join(
            f"{p} {'✅' if x.get('clean') else '⚠️'}" for p, x in (pre.get("panels") or {}).items())
            + (f" · error: {pre['error']}" if pre.get("error") else "")))
    notify.discord("📅 Weekly review + showcase lineup", memo[:3000], fields)
    return {"ideas": len(ids)}
