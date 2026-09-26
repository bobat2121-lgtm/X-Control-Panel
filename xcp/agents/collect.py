"""Scout, part 1: deterministic collection + classification (no LLM)."""
from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import func, select

from xcp import config, db
from xcp.sources import edgar, x_api
from xcp.timeutil import age_hours, today_ny, utcnow

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ classification

@lru_cache(maxsize=2048)
def _kw_re(kw: str) -> re.Pattern:
    return re.compile(r"(?<!\w)\$?" + re.escape(kw.lower()) + r"(?!\w)")


def topic_hits(text: str) -> dict[str, int]:
    """Keyword matches per pillar (only pillars with at least one)."""
    t = (text or "").lower()
    out = {}
    for name, spec in config.pillars().items():
        n = sum(1 for kw in spec.get("keywords", []) if _kw_re(kw).search(t))
        if n:
            out[name] = n
    return out


def classify(text: str, lane_hint: str | None = None, pillar_hint: str | None = None) -> tuple[str, str]:
    pillars = config.pillars()
    if pillar_hint and pillar_hint in pillars:
        return pillars[pillar_hint]["lane"], pillar_hint
    t = (text or "").lower()
    best, best_score = None, 0.0
    for name, spec in pillars.items():
        hits = sum(1 for kw in spec.get("keywords", []) if _kw_re(kw).search(t))
        if not hits:  # the lane hint only breaks ties between pillars that actually matched
            continue
        score = hits + (0.5 if lane_hint and spec.get("lane") == lane_hint else 0.0)
        if score > best_score:
            best, best_score = name, score
    if best is None:
        best = "ai_models" if lane_hint == "ai" else "bitcoin"
    return pillars.get(best, {}).get("lane", lane_hint or "btc"), best


def item_score(kind: str, metrics: dict, created_at, followers: int | None) -> float:
    age = age_hours(created_at)
    if kind == "filing":
        return 100.0 - min(age, 72) / 2
    if kind == "news":
        return max(5.0, 20.0 - age / 2)
    m = metrics or {}
    eng = (m.get("like_count", 0) + 2 * m.get("retweet_count", 0) + 3 * m.get("quote_count", 0)
           + m.get("reply_count", 0) + m.get("bookmark_count", 0))
    velocity = eng / (max(age, 0.25) + 2) ** 1.1
    reach = 1 + math.log10(1 + (followers or 0)) / 10
    return round(velocity * reach, 2)


# ------------------------------------------------------------------ persistence

def upsert_item(s, *, id: str, kind: str, source: str, text: str, url: str = "", author: str = "",
                author_name: str = "", author_followers=None, created_at=None, metrics=None,
                lane_hint=None, pillar_hint=None, meta=None) -> tuple[db.Item, bool]:
    row = s.get(db.Item, id)
    score = item_score(kind, metrics or {}, created_at or utcnow(), author_followers)
    if row is not None:
        row.metrics = metrics or row.metrics
        row.score = score
        row.fetched_at = utcnow()
        return row, False
    lane, pillar = classify(text, lane_hint, pillar_hint)
    meta = {**(meta or {}), "first_seen": utcnow().isoformat(), "hits": topic_hits(text), "hinted": bool(pillar_hint)}
    row = db.Item(id=id, kind=kind, source=source, text=text, url=url, author=author, author_name=author_name,
                  author_followers=author_followers, created_at=created_at, metrics=metrics or {},
                  lane=lane, pillar=pillar, score=score, meta=meta)
    s.add(row)
    return row, True


# ------------------------------------------------------------------ X budget

def _x_reads_since(first_day: str) -> int:
    """Scheduled/on-demand X reads since a NY date. Manual style refreshes are excluded (own cap)."""
    with db.session() as s:
        return int(s.scalar(select(func.coalesce(func.sum(db.Run.x_reads), 0)).where(
            db.Run.run_date >= first_day, db.Run.job != "style_refresh")) or 0)


def x_reads_today() -> int:
    return _x_reads_since(today_ny().isoformat())


def x_reads_this_month() -> int:
    return _x_reads_since(today_ny().replace(day=1).isoformat())


def x_budget(purpose: str = "watchlist") -> int:
    """X reads this run may spend. Keyword searches can't touch the share reserved for watchlist accounts."""
    lim = config.settings().get("limits", {})
    per_run = int(lim.get("x_max_posts_per_run", 100))
    daily = int(lim.get("x_daily_post_cap", 200))
    monthly = int(lim.get("x_monthly_post_cap", 4000))
    if purpose == "search":
        daily -= int(lim.get("x_watchlist_reserve_daily", 0))
        monthly -= int(lim.get("x_watchlist_reserve_monthly", 0))
    return max(0, min(per_run, daily - x_reads_today(), monthly - x_reads_this_month()))


# ------------------------------------------------------------------ collection

def collect(lane: str | None) -> dict:
    """Pull fresh items for a lane ('btc', 'ai' or None for both). Returns stats incl. x_reads."""
    stats = {"x_posts_new": 0, "news_new": 0, "filings_new": 0, "x_reads": 0, "errors": []}
    cfg = config.get("pillars")
    settings = config.settings()

    # --- X
    if x_api.configured():
        budget = x_budget("watchlist")
        search_budget = x_budget("search")
        try:
            client = x_api.XClient()
        except x_api.XError as e:
            stats["errors"].append(str(e))
            client = None
        if client and budget > 0:
            accounts = [a for a in config.get("watchlist").get("accounts", []) if a.get("handle")]
            if lane:
                accounts = [a for a in accounts if a.get("lane", "btc") == lane]
            hints = {a["handle"].lstrip("@").lower(): a for a in accounts}
            jobs = [("watchlist", q, 100, None) for q in x_api.watchlist_queries(
                [a["handle"] for a in accounts], settings.get("limits", {}).get("include_watchlist_replies", False))]
            jobs += [(f"search:{q['name']}", q["query"], int(q.get("max", 50)), q.get("lane"))
                     for q in cfg.get("x_searches", []) if not lane or q.get("lane") == lane]
            with db.session() as s:
                for source, query, max_posts, q_lane in jobs:
                    remaining = (budget if source == "watchlist" else search_budget) - client.reads
                    if remaining < 10:
                        stats["errors"].append("X read budget reached; skipped remaining queries")
                        break
                    key = "x_since:" + hashlib.sha1(query.encode()).hexdigest()[:16]
                    try:
                        posts, newest = client.search_recent(query, min(max_posts, remaining), db.kv_get(key))
                    except x_api.XError as e:
                        stats["errors"].append(f"{source}: {e}")
                        continue
                    for p in posts:
                        hint = hints.get((p["author"] or "").lower(), {})
                        _, new = upsert_item(
                            s, id=f"x:{p['id']}", kind="x_post", source=source, text=p["text"], url=p["url"],
                            author=p["author"], author_name=p["author_name"], author_followers=p["author_followers"],
                            created_at=p["created_at"], metrics=p["metrics"],
                            lane_hint=hint.get("lane") or q_lane or lane, pillar_hint=hint.get("pillar"),
                            meta={"referenced": p["referenced"], "watchlist": source == "watchlist"})
                        stats["x_posts_new"] += int(new)
                    s.commit()
                    if newest:
                        db.kv_set(key, newest)
            stats["x_reads"] = client.reads

    # --- SEC filings (BTC lane)
    if lane in (None, "btc"):
        stats["filings_new"] = len(ingest_filings())

    # --- news feeds and Google News topic searches (shared with the monitor)
    from xcp.agents import monitor

    stats["news_new"] = len(monitor.ingest_news(lane)["new_ids"])
    return stats


def ingest_filings() -> list[str]:
    """New 8-Ks from the issuers in pillars.yaml (Strategy, Strive). Returns the new item ids."""
    new_ids = []
    with db.session() as s:
        known = set(s.scalars(select(db.Item.id).where(db.Item.kind == "filing",
                                                       db.Item.fetched_at >= utcnow() - timedelta(days=10))).all())
        for co in config.get("pillars").get("edgar", []):
            for f in edgar.recent_filings(co["cik"], co["name"], co.get("forms", ["8-K"]), known=known):
                row, new = upsert_item(s, id=f["id"], kind="filing", source=f"edgar:{co['name']}", text=f["text"],
                                       url=f["url"], author=co["name"], author_name=co["name"],
                                       created_at=f["accepted"], lane_hint="btc", pillar_hint="digital_credit",
                                       meta={"form": f["form"], "items": f["items"],
                                             "title": f"{co['name']} filed an {f['form']} (items {f.get('items') or 'n/a'})"})
                if new:
                    new_ids.append(row.id)
        s.commit()
    return new_ids


def select_items(lane: str | None, hours: int = 30, limit: int = 120) -> list[db.Item]:
    since = utcnow() - timedelta(hours=hours)
    with db.session() as s:
        q = select(db.Item).where(db.Item.fetched_at >= since)
        if lane:
            q = q.where(db.Item.lane == lane)
        rows = list(s.scalars(q).all())
    rows = [r for r in rows if age_hours(r.created_at or r.fetched_at) <= hours]
    rows.sort(key=lambda r: r.score, reverse=True)
    filings = [r for r in rows if r.kind == "filing"]
    news = [r for r in rows if r.kind == "news"][: max(10, limit // 5)]
    posts = [r for r in rows if r.kind == "x_post"]
    picked = filings + news
    picked += posts[: max(0, limit - len(picked))]
    return picked
