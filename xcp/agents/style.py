"""Style refresh: pull admired accounts' recent posts and all of your own posts from the X API,
then let Codex extract craft patterns (structure only) into the style library.

Post types: originals and quotes are the craft base; replies are analyzed too (reply craft) and count as
your voice; reposts are someone else's words, kept only as a signal of what gets amplified."""
from __future__ import annotations

import logging

from sqlalchemy import select

from xcp import config, db, llm, notify
from xcp.agents import analyst, collect, context
from xcp.sources import x_api

log = logging.getLogger(__name__)
MIN_REPLY_CHARS = 25  # skip "thanks!" / "lol" style replies


def _uid(client: x_api.XClient, handle: str) -> str:
    key = f"x_user_id:{handle.lower()}"
    uid = db.kv_get(key)
    if not uid:
        uid = client.user_id(handle)
        db.kv_set(key, uid)
    return uid


def post_type(p: dict) -> str:
    refs = {r.get("type") for r in (p.get("referenced") or [])}
    if "retweeted" in refs:
        return "repost"
    if "replied_to" in refs:
        return "reply"
    if "quoted" in refs:
        return "quote"
    return "post"


def _exclude(include_replies: bool, include_reposts: bool) -> str | None:
    ex = [name for name, keep in (("replies", include_replies), ("retweets", include_reposts)) if not keep]
    return ",".join(ex) or None


def _analyze(handle: str, posts: list[dict], n: int = 8) -> int:
    with db.session() as s:
        existing = s.scalars(select(db.StyleExample).where(db.StyleExample.handle == handle,
                                                           db.StyleExample.source == "admired")).all()
        seen = {(r.url, r.hook_type) for r in existing}
        existing_txt = "\n".join(f"- {r.hook_type}: {r.pattern[:120]}" for r in existing) or "(none)"
    lines = [f"{p['id']} | {(p['metrics'] or {}).get('like_count', 0)} | {(p['metrics'] or {}).get('impression_count', 0)} | "
             f"{'[' + post_type(p) + '] ' if post_type(p) != 'post' else ''}{' '.join(p['text'].split())[:500]}"
             for p in posts]

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


def _store_repost(s, owner: str, p: dict) -> bool:
    """Reposts: what an account amplifies. Kept for reference, never used as anyone's voice or craft."""
    if s.scalars(select(db.StyleExample).where(db.StyleExample.source == "repost",
                                               db.StyleExample.url == p["url"])).first():
        return False
    s.add(db.StyleExample(source="repost", handle=owner, url=p["url"], text=p["text"],
                          pillar=collect.classify(p["text"])[1], format="short_observation",
                          hook_type="repost", active=False, strength=5))
    return True


def refresh(analyze: bool = True) -> dict:
    cfg = config.settings().get("style", {})
    per_account = int(cfg.get("posts_per_account", 100))
    extra_n = int(cfg.get("replies_reposts_per_account", 100))
    own_max = int(cfg.get("own_posts_max", 1000))
    inc_replies = bool(cfg.get("include_replies", True))
    inc_reposts = bool(cfg.get("include_reposts", True))
    client = x_api.XClient()
    stats: dict = {"accounts": {}, "entries_added": 0, "own_posts_seen": 0, "own_added": 0, "reposts_saved": 0}

    for a in config.get("watchlist").get("accounts", []):
        h = (a.get("handle") or "").lstrip("@")
        if not h:
            continue
        try:
            uid = _uid(client, h)
            posts = client.user_posts_all(uid, per_account, exclude="retweets,replies")
            if (inc_replies or inc_reposts) and extra_n:
                seen_ids = {p["id"] for p in posts}
                extra = client.user_posts_all(uid, extra_n, exclude=_exclude(inc_replies, inc_reposts))
                posts += [p for p in extra if p["id"] not in seen_ids]
        except x_api.XError as e:
            stats["accounts"][h] = f"error: {e}"
            continue
        kinds = {t: sum(1 for p in posts if post_type(p) == t) for t in ("post", "quote", "reply", "repost")}
        with db.session() as s:
            for p in posts:
                t = post_type(p)
                collect.upsert_item(s, id=f"x:{p['id']}", kind="x_post", source=f"style:{h}:{t}", text=p["text"],
                                    url=p["url"], author=p["author"], author_name=p["author_name"],
                                    author_followers=p["author_followers"], created_at=p["created_at"],
                                    metrics=p["metrics"], lane_hint=a.get("lane"), pillar_hint=a.get("pillar") or None)
                if t == "repost":
                    stats["reposts_saved"] += int(_store_repost(s, h, p))
            s.commit()
        stats["accounts"][h] = kinds
        craft = [p for p in posts if post_type(p) != "repost"
                 and (post_type(p) != "reply" or len(p["text"]) >= MIN_REPLY_CHARS)]
        if analyze and craft:
            try:
                stats["entries_added"] += _analyze(h, craft)
            except llm.LLMError as e:
                stats["accounts"][h] = {**kinds, "analysis": f"failed: {e}"[:200]}

    me = (config.settings().get("account", {}).get("handle") or "").lstrip("@")
    if me:
        try:
            mine = client.user_posts_all(_uid(client, me), own_max, exclude=_exclude(inc_replies, inc_reposts))
            stats["own_posts_seen"] = len(mine)
            with db.session() as s:
                for p in mine:
                    t = post_type(p)
                    if t == "repost":
                        stats["reposts_saved"] += int(_store_repost(s, me, p))
                        continue
                    if t == "reply" and len(p["text"]) < MIN_REPLY_CHARS:
                        continue
                    m = p["metrics"] or {}
                    stats["own_added"] += int(analyst.add_my_post(
                        s, p["url"], p["text"], collect.classify(p["text"])[1],
                        {"likes": m.get("like_count", 0), "views": m.get("impression_count", 0),
                         "reposts": m.get("retweet_count", 0)},
                        fmt="reply" if t == "reply" else ("long_analysis" if len(p["text"]) > 600 else "short_observation"),
                        kind=t))
                s.commit()
        except x_api.XError as e:
            stats["own"] = f"error: {e}"

    stats["x_reads"] = client.reads
    acct = ", ".join(f"{k} ({sum(v.values()) if isinstance(v, dict) and 'analysis' not in v else v})"
                     for k, v in stats["accounts"].items())
    notify.discord("📚 Style library refreshed",
                   f"Accounts: {acct}\nNew craft patterns: {stats['entries_added']}\n"
                   f"Your posts/replies: {stats['own_posts_seen']} read, {stats['own_added']} new in your voice library\n"
                   f"Reposts saved as 'amplified' signals: {stats['reposts_saved']}\n"
                   f"X reads: {client.reads} (≈ ${client.reads * 0.005:.2f})", kind="style")
    return stats
