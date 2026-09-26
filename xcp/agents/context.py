"""Shared context blocks for prompts."""
from __future__ import annotations

import json
import random
from datetime import timedelta

from sqlalchemy import select

from xcp import config, db
from xcp.agents import analyst
from xcp.sources import market
from xcp.timeutil import fmt_ago, today_ny, utcnow


ANALYTICAL_HANDLES = ("RoaringRagnar", "PunterJeff", "ZynxBTC")


def style_block(lane: str | None = None, n: int = 12, seed: str = "", mode: str | None = None) -> str:
    """Your best posts first, then a varied, mostly-short sample of craft patterns from the style library.

    mode "v2" (the human-voice mode): more of your own posts, and only the admired accounts' reasoning moves,
    without their templates, which pull drafts toward the same tidy shape every time."""
    mode = mode or config.settings().get("voice", {}).get("style_mode", "classic")
    if mode == "v2":
        n = min(n, 5)
    with db.session() as s:
        rows = list(s.scalars(select(db.StyleExample).where(db.StyleExample.active.is_(True))).all())
    rng = random.Random(seed or today_ny().isoformat())
    # Your posts: favorites (strength 8+) always, then a rotating sample of the rest so your whole voice gets used.
    all_mine = [r for r in rows if r.source == "mine"]
    n_mine = 16 if mode == "v2" else 10
    favorites = sorted([r for r in all_mine if r.strength >= 8], key=lambda r: -r.strength)[:8 if mode == "v2" else 6]
    others = [r for r in all_mine if r.strength < 8]
    rng.shuffle(others)
    others.sort(key=lambda r: -r.strength)  # stable sort keeps the shuffle within each strength level
    mine = favorites + others[:max(0, n_mine - len(favorites))]
    admired = [r for r in rows if r.source == "admired"]  # reposts are never voice or craft

    analytical = {"quick_analysis", "long_analysis", "data_callout", "contrarian"}

    def rank(r):  # strength, lane fit, a little randomness for variety between runs
        fit = 1.5 if lane and config.pillar_lane(r.pillar) == lane else 0.0
        if mode == "v2":  # lean on the reasoning of the analytical accounts
            fit += (1.5 if r.format in analytical else 0.0) + (1.0 if r.handle in ANALYTICAL_HANDLES else 0.0)
        return r.strength + fit + rng.random() * 2.5

    picked, per_format, longs = [], {}, 0
    for r in sorted(admired, key=rank, reverse=True):
        if len(picked) >= n:
            break
        if per_format.get(r.format, 0) >= 2 or (r.length == "long" and longs >= 2):
            continue
        picked.append(r)
        per_format[r.format] = per_format.get(r.format, 0) + 1
        longs += r.length == "long"

    lines = []
    if mine:
        lines.append("YOUR OWN BEST POSTS (the voice to match; these win any conflict):")
        lines += [f"- {'(reply) ' if r.hook_type == 'reply' else ''}{' '.join(r.text.split())[:500]}" for r in mine]
    if picked and mode == "v2":
        lines.append("REASONING MOVES borrowed from accounts you respect (how to think a post through; never their "
                     "cadence, wording, catchphrases or series; it must still sound like you):")
        lines += [f"- @{r.handle}: {r.pattern}" for r in picked]
    elif picked:
        lines.append("CRAFT PATTERNS learned from accounts you admire (borrow structure only; never reuse their "
                     "wording, facts or jokes):")
        for r in picked:
            lines.append(f"- [{r.format} · {r.length} · hook: {r.hook_type}] {r.pattern} | Template: {r.skeleton} "
                         f"| In your lane: {r.demo}")
    return "\n".join(lines) or "(style library is empty)"


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
    rows = [r for r in rows if "superseded" not in (r.notes or "")]
    return "\n".join(f"{r.date} {r.time + ' ET ' if r.time else ''}{r.title} ({r.pillar})" for r in rows) \
        or "(no events entered)"


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
