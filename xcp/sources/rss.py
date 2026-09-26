from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from xcp.sources.web import html_to_text
from xcp.timeutil import utcnow

log = logging.getLogger(__name__)


def fetch_feed(name: str, url: str, hours: int = 36, limit: int = 25) -> list[dict]:
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 XControlPanel"}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except httpx.HTTPError as e:
        log.info("RSS %s failed: %s", name, e)
        return []
    cutoff = utcnow() - timedelta(hours=hours)
    out = []
    for e in parsed.entries[:limit]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        when = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
        if when and when < cutoff:
            continue
        link = e.get("link", "")
        summary = html_to_text(e.get("summary", ""))[:500]
        out.append({
            "id": "rss:" + hashlib.sha1(link.encode()).hexdigest()[:16],
            "source": name,
            "title": e.get("title", ""),
            "url": link,
            "created_at": when,
            "text": f"{e.get('title', '')}\n{summary}".strip(),
        })
    return out
