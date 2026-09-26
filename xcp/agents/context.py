"""Shared context blocks for prompts."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from xcp import config, db
from xcp.agents import analyst
from xcp.sources import market
from xcp.timeutil import fmt_ago, today_ny, utcnow


def handle() -> str:
    return (config.settings().get("account", {}).get("handle") or "owner").lstrip("@")


def snapshot_block(snap: dict | None = None) -> tuple[str, str, dict]:
    if snap is None:
        row = db.latest_snapshot()
        snap = row.data if row else {}
    flat = market.flatten({k: v for k, v in snap.items() if k not in ("as_of", "as_of_ny")})
    text = "\n".join(f"{k} = {v}" for k, v in flat.items()) or "(no snapshot)"
    return text, snap.get("as_of_ny", "unknown"), flat


def mix_block() -> str:
    m = analyst.mix(days=7)
    lines = [f"Posts in the last 7 days: {m['total']}"]
    for group, target in m["targets"].items():
        share = m["shares"].get(group, 0)
        lines.append(f"- {group}: {share:.0f}% actual vs {target}% target")
    if m["total"] >= 5:
        ai_gap = m["shares"].get("ai", 0) - m["targets"].get("ai", 20)
        if ai_gap > 5:
            lines.append("AI is over target: keep AI takes to the strongest one.")
        elif ai_gap < -10:
            lines.append("AI is under target: an AI angle is welcome if one is genuinely strong.")
    return "\n".join(lines)


def recent_block(days: int = 3) -> str:
    since = utcnow() - timedelta(days=days)
    with db.session() as s:
        posts = s.scalars(select(db.Post).where(db.Post.posted_at >= since).order_by(db.Post.posted_at.desc())
                          .limit(20)).all()
        drafts = s.scalars(select(db.Draft).where(db.Draft.created_at >= since, db.Draft.kind == "regular")
                           .order_by(db.Draft.created_at.desc()).limit(30)).all()
    lines = [f"POSTED {fmt_ago(p.posted_at)}: {p.text[:200]}" for p in posts]
    lines += [f"DRAFTED {d.slot_date} {d.slot}: {d.title}" for d in drafts if d.title]
    return "\n".join(lines) or "(nothing yet)"


def calendar_block(days: int = 7) -> str:
    start = today_ny()
    end = start + timedelta(days=days)
    with db.session() as s:
        rows = s.scalars(select(db.CalendarEvent).where(db.CalendarEvent.date >= start.isoformat(),
                                                         db.CalendarEvent.date <= end.isoformat())
                         .order_by(db.CalendarEvent.date, db.CalendarEvent.time)).all()
    return "\n".join(f"{r.date} {r.time} {r.title} ({r.pillar})" for r in rows) or "(no events entered)"


def items_block(items: list[db.Item], max_chars: int = 600) -> str:
    lines = []
    for it in items:
        m = it.metrics or {}
        eng = (f"likes {m.get('like_count', 0)}, reposts {m.get('retweet_count', 0)}, "
               f"quotes {m.get('quote_count', 0)}, replies {m.get('reply_count', 0)}") if it.kind == "x_post" else "-"
        who = f"@{it.author}" if it.kind == "x_post" else it.author
        if it.author_followers:
            who += f" ({it.author_followers:,})"
        text = " ".join((it.text or "").split())[:max_chars]
        lines.append(f"{it.id} | {it.kind} | {who} | {fmt_ago(it.created_at or it.fetched_at)} | {eng} | {text} | {it.url}")
    return "\n".join(lines) or "(no items; write from the snapshot and calendar)"


def compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
