from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import httpx

from xcp.config import env
from xcp.sources.web import html_to_text
from xcp.timeutil import utcnow

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (compatible; XControlPanel/1.0)"


def google_news_url(query: str, window: str = "1d") -> str:
    q = f"{query} when:{window}" if window else query
    return "https://news.google.com/rss/search?q=" + quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en"


def fetch_feed(name: str, url: str, hours: int = 36, limit: int = 25, ua: str = "") -> list[dict]:
    """Entries newer than `hours`. ua="sec" identifies with SEC_USER_AGENT (SEC fair-access rule)."""
    agent = (env("SEC_USER_AGENT") or UA) if ua == "sec" else UA
    try:
        r = httpx.get(url, headers={"User-Agent": agent}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except httpx.HTTPError as e:
        log.info("RSS %s failed: %s", name, e)
        return []
    google = "news.google.com" in url
    cutoff = utcnow() - timedelta(hours=hours)
    out = []
    for e in parsed.entries[: limit if not google else max(limit, 60)]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        when = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
        if when and when < cutoff:
            continue
        link = e.get("link", "")
        title = (e.get("title") or "").strip()
        publisher = ""
        if google:  # Google News titles end in " - Publisher"; the summary is just a link list
            publisher = ((e.get("source") or {}).get("title") or "").strip()
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3].strip()
            summary = ""
        else:
            summary = html_to_text(e.get("summary", ""))[:500]
        out.append({
            "id": "rss:" + hashlib.sha1(link.encode()).hexdigest()[:16],
            "source": name,
            "publisher": publisher or name,
            "title": title,
            "url": link,
            "created_at": when,
            "text": f"{title}\n{summary}".strip(),
        })
    return out


STOP = set("a an and are as at be by for from has have how in into is it its of on or that the this to was were will "
           "with after over new says said amid why what who as vs its their than more".split())


def title_tokens(title: str) -> set[str]:
    """Normalized words for matching the same story across outlets."""
    words = re.findall(r"\$?[a-z0-9][a-z0-9.\-]*", (title or "").lower())
    return {w.strip(".-") for w in words if w not in STOP and len(w.strip(".-")) > 2}
