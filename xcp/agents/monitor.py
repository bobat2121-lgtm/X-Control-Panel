"""News monitor: what's surfacing right now on X, across news outlets and regulators, and in SEC filings.

Runs every ~15 minutes (.github/workflows/monitor.yml) with no AI:
  1. pulls every feed in pillars.yaml (outlets, Cointelegraph tags, SEC/CFTC/Fed press, Google News topic
     searches), new 8-Ks, and new posts from your watchlist accounts on X (only new posts are billed)
  2. groups the same story across outlets, flags priority items, and pings Discord as they appear
  3. scores everything for the Monitor page: recency first, then source weight, topic fit and momentum
The desk runs at your slot times (xcp/agents/desk.py) turn the best of it into briefs.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from xcp import config, db, notify
from xcp.agents import collect
from xcp.sources import rss, x_api
from xcp.timeutil import age_hours, aware, now_ny, parse_hhmm, parse_iso, utcnow

log = logging.getLogger(__name__)
PINGS = "monitor:pings"
PINGED = "monitor:pinged"
LAST_WATCH_DIGEST = "monitor:watch_digest_at"
PILLAR_COLORS = {"digital_credit": 0xF7931A, "stablecoins": 0x26A17B, "legislation": 0x5B7083, "bitcoin": 0xE8A33D,
                 "macro": 0x5B7083, "ai_models": 0x7C4DFF, "ai_benchmarks": 0x3F51B5, "ai_payments": 0x00ACC1,
                 "physical_ai": 0x00897B}


def cfg() -> dict:
    return config.settings().get("monitor", {})


def watchlist_handles() -> set[str]:
    return {a["handle"].lstrip("@").lower() for a in config.get("watchlist").get("accounts", []) if a.get("handle")}


# ------------------------------------------------------------------ ingest

def feeds(lane: str | None = None) -> list[dict]:
    p = config.get("pillars")
    out = [dict(f) for f in p.get("rss", [])]
    for g in p.get("google_news", []):
        out.append({"name": f"Google News · {g['name']}", "lane": g.get("lane", "btc"), "pillar": g.get("pillar"),
                    "url": rss.google_news_url(g["query"], g.get("window", "1d")), "weight": g.get("weight", 1.5),
                    "google": True})
    return [f for f in out if not lane or f.get("lane") == lane]


def ingest_news(lane: str | None = None) -> dict:
    """Every feed at once (in parallel). Returns new item ids and per-feed counts."""
    hours = int(cfg().get("window_hours", 48))
    fs = feeds(lane)

    def pull(f):
        return f, rss.fetch_feed(f["name"], f["url"], hours=hours, limit=40, ua=f.get("ua", ""))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(pull, fs))
    new_ids, per_feed = [], {}
    with db.session() as s:
        for f, entries in results:
            per_feed[f["name"]] = len(entries)
            for e in entries:
                row, new = collect.upsert_item(
                    s, id=e["id"], kind="news", source=f["name"], text=e["text"], url=e["url"], author=e["publisher"],
                    author_name=e["publisher"], created_at=e["created_at"], lane_hint=f.get("lane"),
                    pillar_hint=f.get("pillar"),
                    meta={"title": e["title"], "weight": f.get("weight", 1), "official": bool(f.get("official")),
                          "feed": f["name"], "google": bool(f.get("google"))})
                if new:
                    new_ids.append(row.id)
        s.commit()
    return {"new_ids": new_ids, "per_feed": per_feed}


def poll_watchlist() -> dict:
    """New posts from your watchlist accounts. Uses the budget reserved for them; empty polls cost nothing."""
    if not (cfg().get("x_watchlist", True) and x_api.configured()):
        return {"new_ids": [], "x_reads": 0, "skipped": "X watchlist polling off or no X token"}
    budget = collect.x_budget("watchlist")
    if budget < 1:
        return {"new_ids": [], "x_reads": 0, "skipped": "X read budget reached"}
    accounts = [a for a in config.get("watchlist").get("accounts", []) if a.get("handle")]
    hints = {a["handle"].lstrip("@").lower(): a for a in accounts}
    include_replies = config.settings().get("limits", {}).get("include_watchlist_replies", False)
    client = x_api.XClient()
    new_ids = []
    with db.session() as s:
        for query in x_api.watchlist_queries([a["handle"] for a in accounts], include_replies):
            remaining = budget - client.reads
            if remaining < 1:
                break
            key = "x_since:" + hashlib.sha1(query.encode()).hexdigest()[:16]  # shared with collect.py
            try:
                posts, newest = client.search_recent(query, min(100, remaining), db.kv_get(key))
            except x_api.XError as e:
                log.warning("watchlist poll failed: %s", e)
                continue
            for p in posts:
                hint = hints.get((p["author"] or "").lower(), {})
                row, new = collect.upsert_item(
                    s, id=f"x:{p['id']}", kind="x_post", source="watchlist", text=p["text"], url=p["url"],
                    author=p["author"], author_name=p["author_name"], author_followers=p["author_followers"],
                    created_at=p["created_at"], metrics=p["metrics"], lane_hint=hint.get("lane"),
                    pillar_hint=hint.get("pillar") or None, meta={"referenced": p["referenced"], "watchlist": True})
                if new:
                    new_ids.append(row.id)
            s.commit()
            if newest:
                db.kv_set(key, newest)
    return {"new_ids": new_ids, "x_reads": client.reads}


# ------------------------------------------------------------------ same story across outlets

def title_of(it: db.Item) -> str:
    return (it.meta or {}).get("title") or (it.text or "").split("\n", 1)[0][:200]


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if inter >= 3 else 0.0


def assign_clusters(new_ids: list[str]) -> None:
    """Point each new news item at an earlier item about the same story (title overlap), else itself."""
    if not new_ids:
        return
    since = utcnow() - timedelta(hours=36)
    with db.session() as s:
        recent = list(s.scalars(select(db.Item).where(db.Item.kind.in_(["news", "filing"]),
                                                      db.Item.fetched_at >= since)).all())
        tokens = {it.id: rss.title_tokens(title_of(it)) for it in recent}
        by_id = {it.id: it for it in recent}
        for iid in new_ids:
            it = by_id.get(iid)
            if it is None or it.kind != "news":
                continue
            mine = tokens.get(iid, set())
            best, best_sim = None, 0.0
            for other in recent:
                if other.id == iid or (other.meta or {}).get("cluster") is None and other.id in new_ids \
                        and new_ids.index(other.id) > new_ids.index(iid):
                    continue
                sim = _similar(mine, tokens.get(other.id, set()))
                if sim > best_sim:
                    best, best_sim = other, sim
            cluster = ((best.meta or {}).get("cluster") or best.id) if best is not None and best_sim >= 0.45 else iid
            it.meta = {**(it.meta or {}), "cluster": cluster}
        s.commit()


def cluster_recent(hours: int = 36) -> None:
    """Group any recent news items that haven't been grouped yet (e.g. collected by a desk run)."""
    since = utcnow() - timedelta(hours=hours)
    with db.session() as s:
        ids = [it.id for it in s.scalars(select(db.Item).where(db.Item.kind == "news", db.Item.fetched_at >= since))
               if not (it.meta or {}).get("cluster")]
    assign_clusters(ids)


# ------------------------------------------------------------------ priority

def _has_word(text: str, word: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I) is not None


def relevance(it: db.Item) -> int:
    """0 = off your lanes (hidden by default). Watchlist posts always count; a topic feed (e.g. Cointelegraph's
    Regulation tag) counts on its own; a Google News result needs a real keyword; altcoin stories need a hit
    in digital credit, stablecoins or legislation to count."""
    m = it.meta or {}
    hits = dict(m.get("hits") or {})
    if m.get("watchlist"):
        return max([1] + list(hits.values()))
    title = title_of(it)
    if any(_has_word(title, w) for w in cfg().get("offlane_words", [])):
        hits = {k: v for k, v in hits.items() if k in ("digital_credit", "stablecoins", "legislation")}
    if hits:
        return max(hits.values())
    return 1 if m.get("hinted") and not m.get("google") else 0


def priority_reason(it: db.Item, cluster_publishers: int = 1) -> str:
    c = cfg()
    m = it.meta or {}
    hits = m.get("hits") or {}
    pri = set(c.get("priority_pillars", ["digital_credit", "stablecoins", "legislation"]))
    on_priority_topic = bool(pri & set(hits)) or it.pillar in pri
    text = title_of(it) if it.kind == "news" else it.text or ""
    breaking = next((w for w in c.get("breaking_words", []) if _has_word(text, w)), None)
    if relevance(it) == 0 and it.kind != "filing":
        return ""
    if it.kind == "filing":
        return f"SEC filing: {title_of(it)}"
    if m.get("official"):
        return f"Official: {m.get('feed', it.source)}"
    if cluster_publishers >= int(c.get("trending_sources", 3)) and relevance(it) > 0:
        return f"Trending: {cluster_publishers} outlets on this in the last 3 hours"
    if breaking and on_priority_topic and it.kind == "news":
        return f"Breaking ({breaking})"
    if breaking and on_priority_topic and m.get("watchlist"):
        return f"@{it.author} ({breaking})"
    return ""


def _quiet(now=None) -> bool:
    start, end = cfg().get("quiet_hours", ["23:00", "06:30"])
    t = (now or now_ny()).time()
    s, e = parse_hhmm(start), parse_hhmm(end)
    return t >= s or t < e if s > e else s <= t < e


def _can_ping() -> bool:
    now = utcnow()
    recent = [t for t in (db.kv_get(PINGS) or []) if (parse_iso(t) or now) > now - timedelta(hours=1)]
    return len(recent) < int(cfg().get("ping_max_per_hour", 5))


def _record_ping(key: str) -> None:
    db.kv_set(PINGS, ((db.kv_get(PINGS) or []) + [utcnow().isoformat()])[-50:])
    db.kv_set(PINGED, ((db.kv_get(PINGED) or []) + [key])[-1000:])


def evaluate(new_ids: list[str], ping: bool = True) -> list[dict]:
    """Flag priority items among the new ones (and stories that just crossed the trending bar); ping Discord."""
    events = []
    since = utcnow() - timedelta(hours=3)
    with db.session() as s:
        recent = list(s.scalars(select(db.Item).where(db.Item.fetched_at >= since)).all())
        clusters: dict[str, set[str]] = {}
        for it in recent:
            if it.kind == "news" and aware(it.created_at or it.fetched_at) >= since:
                clusters.setdefault((it.meta or {}).get("cluster") or it.id, set()).add((it.author or "").lower())
        pinged = set(db.kv_get(PINGED) or [])
        candidates = [it for it in recent if it.id in set(new_ids)]
        # a story can become "trending" when its third outlet lands, even though its lead item isn't new
        for key, pubs in clusters.items():
            if len(pubs) >= int(cfg().get("trending_sources", 3)) and key not in pinged:
                lead = s.get(db.Item, key)
                if lead is not None and lead not in candidates:
                    candidates.append(lead)
        for it in candidates:
            m = it.meta or {}
            key = m.get("cluster") or it.id
            reason = priority_reason(it, len(clusters.get(key, set())))
            if not reason:
                continue
            it.meta = {**m, "priority": reason}
            events.append({"id": it.id, "key": key, "reason": reason, "title": title_of(it), "url": it.url,
                           "author": it.author, "pillar": it.pillar, "kind": it.kind,
                           "published": aware(it.created_at) if it.created_at else None})
        s.commit()
    if ping and cfg().get("ping", True) and not _quiet():
        pinged = set(db.kv_get(PINGED) or [])
        fresh_after = utcnow() - timedelta(hours=float(cfg().get("ping_fresh_hours", 3)))
        for ev in events:
            if ev["key"] in pinged or not _can_ping() or (ev["published"] and ev["published"] < fresh_after):
                continue  # already pinged, over the hourly limit, or old news seen for the first time
            label = config.pillars().get(ev["pillar"], {}).get("label", ev["pillar"])
            notify.discord(f"⚡ {ev['title'][:230]}", f"{ev['reason']} · {label}\n{ev['url']}",
                           color=PILLAR_COLORS.get(ev["pillar"], 0xF7931A))
            _record_ping(ev["key"])
            pinged.add(ev["key"])
    return events


def watchlist_digest(new_ids: list[str]) -> bool:
    """One Discord message listing what your watchlist accounts just posted (at most every N minutes)."""
    minutes = int(cfg().get("watchlist_digest_minutes", 30))
    if not new_ids or not minutes or not cfg().get("ping", True) or _quiet():
        return False
    last = parse_iso(db.kv_get(LAST_WATCH_DIGEST))
    if last and utcnow() - last < timedelta(minutes=minutes):
        return False
    since = last or utcnow() - timedelta(hours=1)
    with db.session() as s:
        rows = list(s.scalars(select(db.Item).where(db.Item.source == "watchlist", db.Item.fetched_at >= since)
                              .order_by(db.Item.created_at.desc())).all())
    rows = [r for r in rows if (r.meta or {}).get("first_seen") and parse_iso(r.meta["first_seen"]) >= since][:10]
    if not rows:
        return False
    fields = [(f"@{r.author}", f"{' '.join((r.text or '').split())[:300]}\n{r.url}") for r in rows]
    notify.discord(f"🎙 Your accounts just posted ({len(rows)})", "", fields, color=0x1DA1F2)
    db.kv_set(LAST_WATCH_DIGEST, utcnow().isoformat())
    return True


# ------------------------------------------------------------------ the run

def run(ping: bool = True) -> dict:
    news = ingest_news()
    filings = collect.ingest_filings()
    watch = poll_watchlist()
    new_ids = news["new_ids"] + filings + watch["new_ids"]
    assign_clusters(news["new_ids"])
    events = evaluate(new_ids, ping=ping)
    if ping:
        watchlist_digest(watch["new_ids"])
    out = {"news_new": len(news["new_ids"]), "filings_new": len(filings), "x_new": len(watch["new_ids"]),
           "x_reads": watch.get("x_reads", 0), "priority": len(events),
           "empty_feeds": [k for k, v in news["per_feed"].items() if not v]}
    db.kv_set("monitor:last_run", {**out, "at": utcnow().isoformat()})
    return out


# ------------------------------------------------------------------ scoring and the stream (Monitor page, desk)

def score(it: db.Item, cluster_publishers: int = 1) -> float:
    """Recency first (half-life 6h), then source weight, topic fit, momentum and priority."""
    m = it.meta or {}
    if it.kind == "filing":
        base = 4.0
    elif it.kind == "x_post":
        mt = it.metrics or {}
        eng = mt.get("like_count", 0) + 2 * mt.get("retweet_count", 0) + 3 * mt.get("quote_count", 0)
        base = 1.0 + min(3.0, math.log10(1 + eng)) + (2.5 if m.get("watchlist") else 0.0)
    else:
        base = float(m.get("weight", 1))
    bonus = min(3, relevance(it)) + min(2.0, 0.5 * (cluster_publishers - 1)) + (3.0 if m.get("priority") else 0.0)
    age = age_hours(it.created_at or parse_iso(m.get("first_seen")) or it.fetched_at)
    return round((base + bonus) * 0.5 ** (age / 6), 3)


def stream(hours: int = 24, kinds: tuple[str, ...] = ("news", "filing", "x_post"), watchlist_only: bool = False,
           include_offtopic: bool = False) -> list[dict]:
    """Stories newest-first: one entry per story, with every outlet that carried it."""
    since = utcnow() - timedelta(hours=hours)
    with db.session() as s:
        rows = list(s.scalars(select(db.Item).where(db.Item.fetched_at >= since, db.Item.kind.in_(list(kinds)))).all())
    rows = [r for r in rows if aware(r.created_at or r.fetched_at) >= since]
    handles = watchlist_handles()
    for r in rows:  # posts collected before the monitor existed carry no flag; tag them in memory by author
        if r.kind == "x_post" and (r.author or "").lower() in handles and not (r.meta or {}).get("watchlist"):
            r.meta = {**(r.meta or {}), "watchlist": True}
    if watchlist_only:
        rows = [r for r in rows if (r.meta or {}).get("watchlist")]
    groups: dict[str, list[db.Item]] = {}
    for r in rows:
        key = (r.meta or {}).get("cluster") or r.id if r.kind == "news" else r.id
        groups.setdefault(key, []).append(r)
    out = []
    for key, members in groups.items():
        members.sort(key=lambda r: aware(r.created_at or r.fetched_at))
        publishers = {(r.author or r.source).lower() for r in members}
        lead = max(members, key=lambda r: (bool((r.meta or {}).get("official")), (r.meta or {}).get("weight", 1),
                                           -aware(r.created_at or r.fetched_at).timestamp()))
        status = next(((r.meta or {}).get("status") for r in members if (r.meta or {}).get("status")), "")
        rel = max(relevance(r) for r in members)
        if not include_offtopic and rel == 0 and lead.kind == "news":
            continue
        newest = max(aware(r.created_at or r.fetched_at) for r in members)
        first_seen = min(parse_iso((r.meta or {}).get("first_seen")) or aware(r.fetched_at) for r in members)
        out.append({"key": key, "lead": lead, "members": members, "publishers": len(publishers),
                    "title": title_of(lead), "newest": newest, "first_seen": first_seen,
                    "priority": next(((r.meta or {}).get("priority") for r in members
                                      if (r.meta or {}).get("priority")), ""),
                    "pillar": lead.pillar, "status": status,
                    "score": max(score(r, len(publishers)) for r in members)})
    return out


def set_status(key: str, status: str) -> None:
    """saved | hidden | used | '' on every item of a story."""
    with db.session() as s:
        for r in s.scalars(select(db.Item).where(db.Item.id == key)).all():
            r.meta = {**(r.meta or {}), "status": status}
        for r in s.scalars(select(db.Item).where(db.Item.fetched_at >= utcnow() - timedelta(days=3))).all():
            if (r.meta or {}).get("cluster") == key:
                r.meta = {**(r.meta or {}), "status": status}
        s.commit()
