"""Style refresh: pull admired accounts' recent posts and all of your own posts from the X API,
then let Codex extract craft patterns (structure only) into the style library."""
from __future__ import annotations

import logging

from sqlalchemy import select

from xcp import config, db, llm, notify
from xcp.agents import analyst, collect, context
from xcp.sources import x_api

log = logging.getLogger(__name__)


def _uid(client: x_api.XClient, handle: str) -> str:
    key = f"x_user_id:{handle.lower()}"
    uid = db.kv_get(key)
    if not uid:
        uid = client.user_id(handle)
        db.kv_set(key, uid)
    return uid


def _analyze(handle: str, posts: list[dict], n: int = 8) -> int:
    with db.session() as s:
        existing = s.scalars(select(db.StyleExample).where(db.StyleExample.handle == handle)).all()
        seen = {(r.url, r.hook_type) for r in existing}
        existing_txt = "\n".join(f"- {r.hook_type}: {r.pattern[:120]}" for r in existing) or "(none)"
    lines = [f"{p['id']} | {(p['metrics'] or {}).get('like_count', 0)} | {(p['metrics'] or {}).get('impression_count', 0)} | "
             f"{' '.join(p['text'].split())[:500]}" for p in posts]

    def mock():
        p = posts[0]
        return {"entries": [{"source_id": p["id"], "format": "short_observation", "length": "short",
                             "pillar": "bitcoin", "hook_type": "[mock] pattern", "pattern": "mock pattern",
                             "skeleton": "[X]. [Y].", "demo": "[mock demo]", "why": "mock", "strength": 5}]}

    prompt = llm.render_prompt("style_analysis", handle=context.handle(), account=handle, posts="\n".join(lines),
                               existing=existing_txt, guidelines=config.get("guidelines"), n=str(n))
    out = llm.run_json(prompt, "style_entries", mock=mock)
    by_id = {p["id"]: p for p in posts}
    added = 0
    with db.session() as s:
        for e in out.get("entries", []):
            src = by_id.get(e["source_id"])
            url = src["url"] if src else f"https://x.com/{handle}/status/{e['source_id']}"
            if (url, e["hook_type"]) in seen:
                continue
            m = (src or {}).get("metrics") or {}
            s.add(db.StyleExample(source="admired", handle=handle, url=url, format=e["format"], length=e["length"],
                                  pillar=e["pillar"], hook_type=e["hook_type"], pattern=e["pattern"],
                                  skeleton=e["skeleton"], demo=e["demo"], why_it_works=e["why"],
                                  metrics={"likes": m.get("like_count", 0), "views": m.get("impression_count", 0)},
                                  strength=max(1, min(10, int(e.get("strength", 7))))))
            seen.add((url, e["hook_type"]))
            added += 1
        s.commit()
    return added


def refresh(analyze: bool = True) -> dict:
    cfg = config.settings().get("style", {})
    per_account = int(cfg.get("posts_per_account", 100))
    own_max = int(cfg.get("own_posts_max", 1000))
    own_exclude = "retweets" if cfg.get("include_own_replies", False) else "retweets,replies"
    client = x_api.XClient()
    stats: dict = {"accounts": {}, "entries_added": 0, "own_posts_seen": 0, "own_added": 0}

    for a in config.get("watchlist").get("accounts", []):
        h = (a.get("handle") or "").lstrip("@")
        if not h:
            continue
        try:
            posts = client.user_posts_all(_uid(client, h), per_account)
        except x_api.XError as e:
            stats["accounts"][h] = f"error: {e}"
            continue
        with db.session() as s:
            for p in posts:
                collect.upsert_item(s, id=f"x:{p['id']}", kind="x_post", source=f"style:{h}", text=p["text"],
                                    url=p["url"], author=p["author"], author_name=p["author_name"],
                                    author_followers=p["author_followers"], created_at=p["created_at"],
                                    metrics=p["metrics"], lane_hint=a.get("lane"), pillar_hint=a.get("pillar") or None)
            s.commit()
        stats["accounts"][h] = len(posts)
        if analyze and posts:
            try:
                stats["entries_added"] += _analyze(h, posts)
            except llm.LLMError as e:
                stats["accounts"][h] = f"{len(posts)} posts, analysis failed: {e}"

    me = (config.settings().get("account", {}).get("handle") or "").lstrip("@")
    if me:
        try:
            mine = client.user_posts_all(_uid(client, me), own_max, exclude=own_exclude)
            stats["own_posts_seen"] = len(mine)
            with db.session() as s:
                for p in mine:
                    m = p["metrics"] or {}
                    stats["own_added"] += int(analyst.add_my_post(
                        s, p["url"], p["text"], collect.classify(p["text"])[1],
                        {"likes": m.get("like_count", 0), "views": m.get("impression_count", 0),
                         "reposts": m.get("retweet_count", 0)},
                        fmt="long_analysis" if len(p["text"]) > 600 else "short_observation"))
                s.commit()
        except x_api.XError as e:
            stats["own"] = f"error: {e}"

    stats["x_reads"] = client.reads
    notify.discord("📚 Style library refreshed",
                   f"Accounts: {', '.join(f'{k} ({v})' for k, v in stats['accounts'].items())}\n"
                   f"New craft patterns: {stats['entries_added']}\n"
                   f"Your posts: {stats['own_posts_seen']} read, {stats['own_added']} new in your voice library\n"
                   f"X reads: {client.reads} (≈ ${client.reads * 0.005:.2f})")
    return stats
