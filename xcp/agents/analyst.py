"""Analyst: your posts, their metrics, and the 80/20 mix. No LLM needed."""
from __future__ import annotations

import logging
from datetime import timedelta
from difflib import SequenceMatcher

from sqlalchemy import select

from xcp import config, db, xtext
from xcp.sources import x_api
from xcp.timeutil import utcnow

log = logging.getLogger(__name__)


def mix(days: int = 7) -> dict:
    targets = config.settings().get("targets", {}).get("pillars", {})
    tone_targets = config.settings().get("targets", {}).get("tones", {})
    since = utcnow() - timedelta(days=days)
    with db.session() as s:
        posts = s.scalars(select(db.Post).where(db.Post.posted_at >= since)).all()
    counts: dict[str, int] = {k: 0 for k in targets}
    tones: dict[str, int] = {k: 0 for k in tone_targets}
    for p in posts:
        g = config.target_group(p.pillar)
        counts[g] = counts.get(g, 0) + 1
        if p.tone:
            tones[p.tone] = tones.get(p.tone, 0) + 1
    total = sum(counts.values())
    shares = {k: (v * 100 / total if total else 0.0) for k, v in counts.items()}
    tone_total = sum(tones.values())
    tone_shares = {k: (v * 100 / tone_total if tone_total else 0.0) for k, v in tones.items()}
    return {"total": total, "counts": counts, "shares": shares, "targets": targets,
            "tones": tones, "tone_shares": tone_shares, "tone_targets": tone_targets}


def log_post(draft_id: int | None, url: str, text: str, pillar: str, tone: str | None, style: str | None,
             kind: str = "regular") -> str:
    """Record a post you made (from the Feed's 'Mark posted')."""
    tid = xtext.tweet_id_from_url(url) or f"local-{utcnow().timestamp():.0f}"
    with db.session() as s:
        row = s.get(db.Post, tid)
        if row is None:
            row = db.Post(id=tid)
            s.add(row)
        row.url, row.text, row.draft_id = url, text, draft_id
        row.posted_at = row.posted_at or utcnow()
        row.pillar, row.lane = pillar, config.pillar_lane(pillar)
        row.tone, row.style, row.kind, row.source = tone, style, kind, "panel"
        s.commit()
    return tid


def _attribute(s, text: str) -> db.Draft | None:
    """Find the draft a post came from, if any (fuzzy match on recent drafts)."""
    since = utcnow() - timedelta(days=4)
    drafts = s.scalars(select(db.Draft).where(db.Draft.created_at >= since)).all()
    best, best_ratio = None, 0.0
    for d in drafts:
        for v in db.current_variants(s, d.id):
            r = SequenceMatcher(None, xtext.join_parts(v.parts)[:600].lower(), text[:600].lower()).ratio()
            if r > best_ratio:
                best, best_ratio = d, r
    return best if best_ratio >= 0.55 else None


def import_own_posts(days: int = 8) -> dict:
    """Nightly: pull your recent posts + metrics from the X API (cheap owned reads)."""
    handle = (config.settings().get("account", {}).get("handle") or "").lstrip("@")
    if not handle or not x_api.configured():
        return {"skipped": "set account.handle and X_BEARER_TOKEN to track your posts", "x_reads": 0}
    from xcp.agents.collect import classify

    client = x_api.XClient()
    uid = db.kv_get(f"x_user_id:{handle}")
    if not uid:
        uid = client.user_id(handle)
        db.kv_set(f"x_user_id:{handle}", uid)
    posts = client.user_posts(uid, max_posts=60, since=utcnow() - timedelta(days=days))
    new = 0
    with db.session() as s:
        for p in posts:
            m = p["metrics"] or {}
            row = s.get(db.Post, p["id"])
            if row is None:
                draft = _attribute(s, p["text"])
                lane, pillar = classify(p["text"])
                row = db.Post(id=p["id"], url=p["url"], text=p["text"], posted_at=p["created_at"], source="x_api",
                              lane=lane, pillar=pillar)
                if draft:
                    row.draft_id, row.pillar, row.lane, row.tone = draft.id, draft.pillar, draft.lane, draft.tone
                    row.kind = draft.kind
                    if draft.status != "posted":
                        draft.status, draft.posted_url, draft.posted_at = "posted", p["url"], p["created_at"]
                s.add(row)
                new += 1
            else:
                row.text = p["text"]
                row.posted_at = row.posted_at or p["created_at"]
                if row.url.startswith("local-") or not row.url:
                    row.url = p["url"]
            snap = {"impressions": m.get("impression_count", 0), "likes": m.get("like_count", 0),
                    "reposts": m.get("retweet_count", 0), "replies": m.get("reply_count", 0),
                    "quotes": m.get("quote_count", 0), "bookmarks": m.get("bookmark_count", 0)}
            row.latest_metrics = snap
            s.add(db.PostMetric(post_id=p["id"], **snap))
        s.commit()
    return {"posts_seen": len(posts), "posts_new": new, "x_reads": client.reads}
