"""Desk: at your slot times, the AI turns what the monitor found into briefs you write from.

No post drafts: each brief is what happened, why it matters for your lanes, the numbers (checked against the
sources), and 2-3 questions to spark your own take, plus X posts worth replying to. Settings → writer.mode
"drafts" switches the slots back to AI-written drafts.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from xcp import config, db, llm, notify
from xcp.agents import collect, context, editor, monitor
from xcp.sources import market
from xcp.timeutil import dayname, fmt_ago, now_ny, today_ny, utcnow

log = logging.getLogger(__name__)

DESK_BRIEFS = {
    "premarket": "Pre-market: what happened overnight and what matters before the 9:30 open. On Mondays, the "
                 "Strategy and Strive 8-Ks come first.",
    "ai_noon": "AI lane: frontier model releases and benchmarks, AI agents paying with stablecoins, physical AI.",
    "midday": "Midday, market open: what's moving now, and anything that broke this morning.",
    "friday_close": "Friday after the close: the week's biggest stories and what's set up for next week.",
}
HOURS = {"premarket": 16, "ai_noon": 24, "midday": 6, "friday_close": 8}
N_STORIES = {"premarket": 6, "ai_noon": 4, "midday": 5, "friday_close": 5}


def _item_line(it: db.Item, publishers: int, max_chars: int) -> str:
    m = it.meta or {}
    tags = []
    if m.get("watchlist"):
        tags.append("[watchlist]")
    if m.get("priority"):
        tags.append(f"[priority: {m['priority']}]")
    if publishers > 1:
        tags.append(f"[+{publishers - 1} outlets]")
    mt = it.metrics or {}
    eng = (f"likes {mt.get('like_count', 0)}, reposts {mt.get('retweet_count', 0)}, quotes {mt.get('quote_count', 0)}"
           if it.kind == "x_post" else "-")
    who = f"@{it.author}" if it.kind == "x_post" else (it.author or it.source)
    if it.author_followers:
        who += f" ({it.author_followers:,})"
    text = " ".join((it.text or "").split())[:max_chars]
    return f"{it.id} | {it.kind} | {who} | {fmt_ago(it.created_at or it.fetched_at)} | {eng} | {' '.join(tags)} {text} | {it.url}"


def _recent_block() -> str:
    since = utcnow() - timedelta(hours=24)
    with db.session() as s:
        rows = s.scalars(select(db.Brief).where(db.Brief.created_at >= since, db.Brief.kind == "story")
                         .order_by(db.Brief.created_at.desc())).all()
    return "\n".join(f"- {b.title}" for b in rows) or "(none yet)"


def run_desk(slot: str) -> dict:
    settings = config.settings()
    spec = settings["slots"][slot]
    lane = spec.get("lane", "btc")
    d, now = today_ny(), now_ny()
    stats: dict = {"slot": slot, "mode": "monitor"}

    snap = market.take_snapshot()
    snap_text, snap_time, flat = context.snapshot_block(snap)
    stats["collect"] = collect.collect(None)  # X (watchlist first, then searches), 8-Ks, every feed
    stats["x_reads"] = stats["collect"].get("x_reads", 0)
    monitor.cluster_recent()

    hours = int(spec.get("desk_hours") or HOURS.get(slot, 8))
    stories = monitor.stream(hours=hours)
    for c in stories:  # the slot's lane leads (the noon slot is the AI lane)
        c["rank"] = c["score"] * (1.5 if c["lead"].lane == lane else 1.0)
    stories.sort(key=lambda c: -c["rank"])
    limit = int(settings.get("limits", {}).get("items_to_llm", 120))
    max_chars = int(settings.get("limits", {}).get("item_text_chars", 600))
    lines, by_id = [], {}
    for c in stories:
        for it in [c["lead"]] + [m for m in c["members"] if m is not c["lead"]][:2]:
            if len(lines) >= limit:
                break
            lines.append(_item_line(it, c["publishers"], max_chars))
            by_id[it.id] = it
    n = int(spec.get("desk_stories") or N_STORIES.get(slot, 5))
    watch = ", ".join("@" + a["handle"].lstrip("@") for a in config.get("watchlist").get("accounts", []))
    prompt = llm.render_prompt(
        "desk", handle=context.handle(), slot_label=spec.get("label", slot), weekday=dayname(d).title(),
        date=d.isoformat(), time=now.strftime("%H:%M"), slot_brief=DESK_BRIEFS.get(slot, ""), watchlist=watch,
        snapshot=snap_text, snapshot_time=snap_time, calendar=context.calendar_block(), recent=_recent_block(),
        items="\n".join(lines) or "(nothing new in this window)", n_stories=str(n))

    def mock() -> dict:
        return {"stories": [{"title": c["title"][:90], "what": " ".join((c["lead"].text or "").split())[:200],
                             "why": "[mock]", "numbers": [], "angles": ["[mock] What's your take?"],
                             "pillar": c["pillar"] if c["pillar"] in config.pillars() else "bitcoin",
                             "priority": 3 if c["priority"] else 2, "item_ids": [c["lead"].id]} for c in stories[:n]],
                "reply_targets": []}

    out = llm.run_json(prompt, "desk", mock=mock)
    known = [it.text or "" for it in by_id.values()]
    saved = []
    with db.session() as s:
        for st_ in out.get("stories", [])[:n]:
            items = [by_id[i] for i in st_.get("item_ids", []) if i in by_id]
            numbers_text = [f"{x['label']} {x['value']}" for x in st_.get("numbers", [])]
            flags = [f for f in editor.check_variant([st_["what"], st_["why"]] + numbers_text, flat, known, robot=False)
                     if f.startswith("Unverified number")]
            b = db.Brief(run_slot=slot, run_date=d.isoformat(), kind="story", title=st_["title"][:300],
                         what=st_["what"], why=st_["why"], numbers=st_.get("numbers", []),
                         angles=st_.get("angles", [])[:3], pillar=st_["pillar"], lane=config.pillar_lane(st_["pillar"]),
                         priority=int(st_.get("priority", 2)), item_ids=[it.id for it in items], flags=flags,
                         sources=[{"title": monitor.title_of(it)[:200], "url": it.url,
                                   "publisher": ("@" + it.author) if it.kind == "x_post" else (it.author or it.source),
                                   "kind": it.kind} for it in items])
            s.add(b)
            saved.append(b)
        for rt in out.get("reply_targets", [])[:3]:
            it = by_id.get(rt.get("item_id"))
            if it is None or it.kind != "x_post":
                continue
            s.add(db.Brief(run_slot=slot, run_date=d.isoformat(), kind="reply_target",
                           title=f"@{it.author}: {' '.join((it.text or '').split())[:200]}", why=rt.get("why", ""),
                           pillar=it.pillar, lane=it.lane, priority=2, item_ids=[it.id],
                           sources=[{"title": (it.text or "")[:200], "url": it.url, "publisher": "@" + it.author,
                                     "kind": "x_post"}]))
        s.commit()
    stats.update({"briefs": len(saved), "reply_targets": len(out.get("reply_targets", [])[:3]),
                  "items_seen": len(lines)})
    _digest(spec, saved)
    return stats


def _digest(spec: dict, briefs: list[db.Brief]) -> None:
    if not briefs:
        return
    top = sorted(briefs, key=lambda b: -b.priority)[:5]
    fields = []
    for b in top:
        link = next((x["url"] for x in b.sources if x.get("url")), "")
        fields.append((f"{'⚡' * max(1, b.priority - 1)} {b.title[:200]}",
                       f"{b.what[:450]}" + (f"\n{link}" if link else "")))
    notify.discord(f"🗞 {spec.get('label', 'Desk')} brief: {len(briefs)} stories to write from",
                   "Your Monitor page has the full briefs, numbers and angle questions.", fields)
