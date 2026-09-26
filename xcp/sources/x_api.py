"""X API v2 (pay-per-use). Reads only; posting happens through X's composer."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from xcp.config import env
from xcp.timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)
BASE = "https://api.x.com/2"
TWEET_FIELDS = "created_at,public_metrics,author_id,conversation_id,referenced_tweets,note_tweet,lang"
USER_FIELDS = "username,name,public_metrics,verified"


class XError(RuntimeError):
    pass


def configured() -> bool:
    return bool(env("X_BEARER_TOKEN"))


class XClient:
    def __init__(self, bearer: str | None = None):
        self.bearer = bearer or env("X_BEARER_TOKEN")
        if not self.bearer:
            raise XError("X_BEARER_TOKEN is not set")
        self.reads = 0  # posts returned (what pay-per-use bills)
        self.http = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {self.bearer}"}, timeout=30)

    def _get(self, path: str, **params) -> dict:
        r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        if r.status_code == 429:
            raise XError("rate limited (429)")
        if r.status_code >= 400:
            raise XError(f"{r.status_code}: {r.text[:300]}")
        return r.json()

    @staticmethod
    def _normalize(payload: dict) -> list[dict]:
        users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
        out = []
        for t in payload.get("data", []) or []:
            u = users.get(t.get("author_id"), {})
            username = u.get("username", "")
            text = (t.get("note_tweet") or {}).get("text") or t.get("text", "")
            out.append({
                "id": t["id"],
                "text": text,
                "created_at": parse_iso(t.get("created_at")),
                "author": username,
                "author_name": u.get("name", ""),
                "author_followers": (u.get("public_metrics") or {}).get("followers_count"),
                "metrics": t.get("public_metrics", {}),
                "url": f"https://x.com/{username or 'i'}/status/{t['id']}",
                "referenced": t.get("referenced_tweets", []),
                "conversation_id": t.get("conversation_id"),
            })
        return out

    def search_recent(self, query: str, max_posts: int = 50, since_id: str | None = None,
                      hours_back: int = 18) -> tuple[list[dict], str | None]:
        """Returns (posts, newest_id). Stops at max_posts to control spend."""
        posts: list[dict] = []
        newest = None
        token = None
        start_time = None if since_id else (utcnow() - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        while len(posts) < max_posts:
            batch = min(100, max(10, max_posts - len(posts)))
            try:
                payload = self._get("/tweets/search/recent", query=query, max_results=batch, since_id=since_id,
                                    start_time=start_time, next_token=token, **{
                                        "tweet.fields": TWEET_FIELDS, "expansions": "author_id",
                                        "user.fields": USER_FIELDS})
            except XError as e:
                if since_id and "since_id" in str(e):  # too old -> fall back to a time window
                    return self.search_recent(query, max_posts, None, hours_back)
                raise
            got = self._normalize(payload)
            self.reads += len(got)
            posts.extend(got)
            meta = payload.get("meta", {})
            newest = newest or meta.get("newest_id")
            token = meta.get("next_token")
            if not token or not got:
                break
        return posts, newest

    def get_tweet(self, tweet_id: str) -> dict | None:
        payload = self._get(f"/tweets/{tweet_id}", **{"tweet.fields": TWEET_FIELDS, "expansions": "author_id",
                                                      "user.fields": USER_FIELDS})
        payload["data"] = [payload["data"]] if payload.get("data") else []
        got = self._normalize(payload)
        self.reads += len(got)
        return got[0] if got else None

    def user_id(self, username: str) -> str | None:
        payload = self._get(f"/users/by/username/{username.lstrip('@')}")
        return (payload.get("data") or {}).get("id")

    def user_posts(self, user_id: str, max_posts: int = 40, since: datetime | None = None) -> list[dict]:
        payload = self._get(f"/users/{user_id}/tweets", max_results=min(100, max(5, max_posts)),
                            exclude="retweets,replies",
                            start_time=since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
                            **{"tweet.fields": TWEET_FIELDS, "expansions": "author_id", "user.fields": USER_FIELDS})
        got = self._normalize(payload)
        self.reads += len(got)
        return got


    def user_posts_all(self, user_id: str, max_posts: int = 100, exclude: str | None = "retweets,replies") -> list[dict]:
        """Most recent posts from one account, paginated (X serves up to ~3,200 per timeline)."""
        posts: list[dict] = []
        token = None
        while len(posts) < max_posts:
            payload = self._get(f"/users/{user_id}/tweets", max_results=min(100, max(5, max_posts - len(posts))),
                                exclude=exclude or None, pagination_token=token,
                                **{"tweet.fields": TWEET_FIELDS, "expansions": "author_id", "user.fields": USER_FIELDS})
            got = self._normalize(payload)
            self.reads += len(got)
            posts.extend(got)
            token = (payload.get("meta") or {}).get("next_token")
            if not token or not got:
                break
        return posts[:max_posts]


def watchlist_queries(handles: list[str], include_replies: bool = False, max_len: int = 480) -> list[str]:
    """Chunk handles into 'from:a OR from:b' queries under X's query length limit."""
    suffix = " -is:retweet" + ("" if include_replies else " -is:reply")
    queries, cur = [], []
    for h in [h.strip().lstrip("@") for h in handles if h and h.strip()]:
        cand = cur + [f"from:{h}"]
        if len("(" + " OR ".join(cand) + ")" + suffix) > max_len and cur:
            queries.append("(" + " OR ".join(cur) + ")" + suffix)
            cur = [f"from:{h}"]
        else:
            cur = cand
    if cur:
        queries.append("(" + " OR ".join(cur) + ")" + suffix)
    return queries
