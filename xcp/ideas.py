"""Post ideas, grouped under the slot times you post at (the Monitor page's idea feed).

An idea is either a desk brief (researched at the slot's run time) or a live story from the monitor. Briefs sit
under the slot they were written for. A live story goes to the next upcoming slot in its lane (the AI lane goes to
the AI slot, everything else to the next BTC slot), and drops out once a brief already covers it.
Ideas are plain dicts (JSON-safe) so the Writer tab can pin them.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from xcp import config, db, showcase
from xcp.sources import digital_exposure as de
from xcp.timeutil import at_ny, aware, days_match, now_ny, parse_iso, utcnow

HORIZON_HOURS = 36  # upcoming slots shown ahead, plus the next slot of every lane however far away
MIN_WINDOW_HOURS = {"btc": 24, "ai": 36}  # a live story is an idea for at least this long
PRIORITY_LABEL = {3: "post now", 2: "today", 1: "worth knowing"}


def occurrences(now: datetime | None = None) -> list[dict]:
    """Slots coming up in the next 36 hours plus the next slot of every lane (soonest first), then the slots of the
    last 36 hours (latest first; the page shows those only when they hold desk picks)."""
    now = now or now_ny()
    slots = config.settings().get("slots", {})
    seen, out = set(), []

    def add(d, name, spec):
        k = f"{d.isoformat()}_{name}"
        if k in seen:
            return
        seen.add(k)
        post = at_ny(d, spec["post_at"])
        panel = showcase.panel_for(d) if name in showcase.showcase_slots(d) else None
        out.append({"key": k, "slot": name, "date": d.isoformat(), "post_at": post, "run_at": spec.get("run_at", ""),
                    "label": spec.get("label", name), "lane": spec.get("lane", "btc"), "passed": post <= now,
                    "panel": panel})

    for i in range(-2, 3):
        d = (now + timedelta(days=i)).date()
        for name, spec in slots.items():
            if not days_match(spec.get("days"), d) or not spec.get("post_at"):
                continue
            post = at_ny(d, spec["post_at"])
            if now - timedelta(hours=HORIZON_HOURS) <= post <= now + timedelta(hours=HORIZON_HOURS):
                add(d, name, spec)
    for lane in {s.get("lane", "btc") for s in slots.values()}:  # e.g. Friday night → Monday's pre-market
        if not any(o["lane"] == lane and not o["passed"] for o in out):
            for i in range(8):
                d = (now + timedelta(days=i)).date()
                nxt = sorted((at_ny(d, s["post_at"]), n, s) for n, s in slots.items()
                             if s.get("lane", "btc") == lane and s.get("post_at") and days_match(s.get("days"), d)
                             and at_ny(d, s["post_at"]) > now)
                if nxt:
                    add(d, nxt[0][1], nxt[0][2])
                    break
    upcoming = sorted((o for o in out if not o["passed"]), key=lambda o: o["post_at"])
    passed = sorted((o for o in out if o["passed"]), key=lambda o: o["post_at"], reverse=True)
    return upcoming + passed


# ------------------------------------------------------------------ building ideas

def _snippet(it: db.Item, n: int = 420) -> str:
    text = it.text or ""
    if it.kind == "x_post":
        return " ".join(text.split())[:n]
    head, _, body = text.partition("\n")
    body = " ".join(body.split())
    if not body or body.lower().startswith(" ".join(head.split()).lower()[:40].rstrip(". ")):
        return ""  # Google News repeats the headline as the summary
    return body[:n]


def related(idea: dict, pool: list[dict], n: int = 5) -> list[dict]:
    """Other ideas on the same story (clustering keeps some apart: different wording, different day)."""
    from xcp.sources.rss import title_tokens

    toks = title_tokens(idea["title"])
    if len(toks) < 2:
        return []
    scored = []
    for x in pool:
        if x["key"] == idea["key"]:
            continue
        other = title_tokens(x["title"])
        shared = len(toks & other)
        if shared >= 2 and shared / max(1, len(toks | other)) >= 0.18:
            scored.append((shared, x["newest"], x))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [x for _, _, x in scored[:n]]


# ------------------------------------------------------------------ when did the event first surface?

ORIGIN_DAYS = 14  # how far back to look for earlier coverage (by publish time)
ORIGIN_SIM = 0.45  # the same bar the monitor uses to group outlets into one story
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(title: str) -> set[str]:
    """Figures in a headline ('$950M' -> '950', '2,500' -> '2500'); years don't count."""
    out = set()
    for m in _NUM.findall(title or ""):
        n = m.replace(",", "")
        if n.isdigit() and len(n) == 4 and 1990 <= int(n) <= 2100:
            continue
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


class EventIndex:
    """Recent coverage, searchable by headline: finds the earliest report of an event even when the monitor filed
    a later repost as its own story (different wording, or outside its 36-hour grouping window)."""

    def __init__(self, rows: list[dict]):
        from xcp.sources.rss import title_tokens

        self.rows: list[dict] = []
        self.postings: dict[str, list[int]] = {}
        for r in rows:
            toks = title_tokens(r["title"])
            if len(toks) < (5 if r.get("kind") == "x_post" else 3) or r.get("at") is None:
                continue  # an X post only counts as a first report when it reads like a headline
            i = len(self.rows)
            self.rows.append({**r, "toks": toks, "nums": _numbers(r["title"])})
            for t in toks:
                self.postings.setdefault(t, []).append(i)
        self.max_df = max(40, len(self.rows) // 20)  # words in >5% of headlines ('bitcoin') can't nominate a match
        self._memo: dict = {}

    def origin(self, title: str, before: datetime | None, hops: int = 2) -> dict | None:
        """The earliest earlier report of the same event, or None. Two headlines that both carry figures but share
        none are different events (this week's buy vs last week's)."""
        from xcp.sources.rss import title_tokens

        if before is None:
            return None
        key = (title, before, hops)
        if key in self._memo:
            return self._memo[key]
        toks, nums = title_tokens(title), _numbers(title)
        best = None
        if len(toks) >= 3:
            votes: dict[int, int] = {}
            for t in toks:
                post = self.postings.get(t, ())
                if len(post) <= self.max_df:
                    for i in post:
                        votes[i] = votes.get(i, 0) + 1
            for i, n in votes.items():
                if n < 2:
                    continue
                r = self.rows[i]
                if r["at"] >= before - timedelta(minutes=1):
                    continue
                shared = len(toks & r["toks"])
                if shared < 3 or shared / len(toks | r["toks"]) < ORIGIN_SIM:
                    continue
                if nums and r["nums"] and not nums & r["nums"]:
                    continue
                if best is None or r["at"] < best["at"]:
                    best = r
            if best is not None and hops > 1:  # a repost of a repost: follow it back once more
                earlier = self.origin(best["title"], best["at"], hops - 1)
                if earlier is not None and earlier["at"] < best["at"]:
                    best = earlier
        self._memo[key] = best
        return best


def load_event_index(days: int = ORIGIN_DAYS) -> EventIndex:
    """Headlines and X posts published in the last `days` (one light query: no bodies beyond an X post's text)."""
    since = utcnow() - timedelta(days=days)
    when = func.coalesce(db.Item.created_at, db.Item.fetched_at)
    with db.session() as s:
        rows = s.execute(select(db.Item.kind, db.Item.text, db.Item.meta, db.Item.created_at, db.Item.fetched_at,
                                db.Item.author, db.Item.source, db.Item.url)
                         .where(when >= since, db.Item.kind.in_(["news", "filing", "x_post"]))).all()
    return EventIndex([{"title": (meta or {}).get("title") or (text or "").split("\n", 1)[0][:200],
                        "at": aware(created or fetched), "url": url or "", "kind": kind,
                        "publisher": f"@{author}" if kind == "x_post" else (author or source or "")}
                       for kind, text, meta, created, fetched, author, source, url in rows])


def stamp(idea: dict, index: EventIndex | None = None) -> dict:
    """Set idea['surfaced'] (when the underlying event first surfaced: its earliest coverage anywhere we've seen)
    and idea['surfaced_via'] (that first report). 'newest' stays the latest coverage."""
    dated = [x for x in idea.get("items", []) if x.get("at")]
    first = min(dated, key=lambda x: parse_iso(x["at"])) if dated else None
    at = parse_iso(first["at"]) if first else parse_iso(idea.get("newest"))
    via = first
    if index is not None:  # search by headlines only: a post's first line isn't a headline
        for x in [x for x in dated if x.get("kind") != "x_post"][:6]:
            o = index.origin(x["title"], parse_iso(x["at"]))
            if o is not None and (at is None or o["at"] < at):
                at, via = o["at"], {"title": o["title"], "url": o["url"], "publisher": o["publisher"],
                                    "kind": o["kind"], "at": o["at"].isoformat()}
    idea["surfaced"] = at.isoformat() if at else None
    idea["surfaced_via"] = {k: via.get(k) for k in ("title", "url", "publisher", "kind", "at")} if via else None
    return idea


def _publisher(it: db.Item) -> str:
    return f"@{it.author}" if it.kind == "x_post" else (it.author or it.source or "")


def item_dict(it: db.Item) -> dict:
    from xcp.agents import monitor

    m = it.meta or {}
    return {"title": monitor.title_of(it), "url": it.url or "", "publisher": _publisher(it), "kind": it.kind,
            "official": bool(m.get("official")), "watchlist": bool(m.get("watchlist")),
            "at": aware(it.created_at or it.fetched_at).isoformat(), "snippet": _snippet(it),
            "metrics": (it.metrics or {}) if it.kind == "x_post" else {}}


def from_story(c: dict) -> dict:
    lead = c["lead"]
    members = sorted(c["members"], key=lambda r: (r is not lead, -aware(r.created_at or r.fetched_at).timestamp()))
    return {"key": f"s:{c['key']}", "kind": "story", "story_key": c["key"], "brief_id": None,
            "title": c["title"], "pillar": c["pillar"], "priority": c["priority"] or "", "hot": bool(c["priority"]),
            "what": "", "why": "", "numbers": [], "angles": [], "flags": [],
            "items": [item_dict(r) for r in members],
            "quote_url": lead.url if lead.kind == "x_post" else None,
            "newest": c["newest"].isoformat(), "first_seen": (c["first_seen"] or c["newest"]).isoformat(),
            "status": c["status"] or "", "score": c["score"], "publishers": c["publishers"],
            "watchlist": bool((lead.meta or {}).get("watchlist"))}


def from_brief(b: db.Brief, items: dict[str, db.Item]) -> dict:
    its = [item_dict(items[i]) for i in b.item_ids or [] if i in items]
    known = {x["url"] for x in its}
    for x in b.sources or []:  # sources whose items aren't loaded still show as links
        if x.get("url") and x["url"] not in known:
            its.append({"title": x.get("title", ""), "url": x["url"], "publisher": x.get("publisher", ""),
                        "kind": x.get("kind", "news"), "official": False, "watchlist": False, "at": None,
                        "snippet": "", "metrics": {}})
    created = aware(b.created_at).isoformat()
    return {"key": f"b:{b.id}", "kind": "brief", "story_key": None, "brief_id": b.id, "title": b.title,
            "pillar": b.pillar, "priority": PRIORITY_LABEL.get(b.priority, ""), "hot": b.priority >= 3,
            "what": b.what or "", "why": b.why or "", "numbers": b.numbers or [], "angles": b.angles or [],
            "flags": b.flags or [], "items": its,
            "quote_url": next((x["url"] for x in its if x["kind"] == "x_post"), None) if b.kind == "reply_target"
            else None, "newest": created, "first_seen": created, "status": b.status or "",
            "score": 100.0 + b.priority, "publishers": len({x["publisher"] for x in its}),
            "watchlist": any(x["watchlist"] for x in its), "item_ids": list(b.item_ids or []),
            "run_slot": b.run_slot, "run_date": b.run_date}


def load_briefs(since_hours: int = 60, statuses_out: tuple[str, ...] = ("dismissed",)) -> list[dict]:
    since = utcnow() - timedelta(hours=since_hours)
    with db.session() as s:
        rows = list(s.scalars(select(db.Brief).where(db.Brief.created_at >= since, db.Brief.kind == "story",
                                                     db.Brief.status.not_in(list(statuses_out)))
                              .order_by(db.Brief.created_at.desc())).all())
        ids = {i for b in rows for i in (b.item_ids or [])}
        items = {it.id: it for it in s.scalars(select(db.Item).where(db.Item.id.in_(list(ids)))).all()} if ids else {}
    return [from_brief(b, items) for b in rows]


def assign(stories: list[dict], briefs: list[dict], occs: list[dict], now: datetime | None = None,
           index: EventIndex | None = None) -> dict[str, list]:
    """occurrence key -> ideas, best first (desk picks, then priority stories, then by score). With an index, every
    idea is stamped with when its event first surfaced."""
    now = now or now_ny()
    out: dict[str, list] = {o["key"]: [] for o in occs}
    covered: set[str] = set()
    slots = config.settings().get("slots", {})
    for b in briefs:
        if b["status"] == "dismissed":  # passed on since it was loaded
            continue
        k = f"{b['run_date']}_{b['run_slot']}"
        if k not in out:  # a manual run on a day that slot doesn't post: the next slot in its lane takes it
            lane = slots.get(b["run_slot"], {}).get("lane") or config.pillar_lane(b["pillar"])
            fresh = parse_iso(b["newest"]) > utcnow() - timedelta(hours=HORIZON_HOURS)
            k = next((o["key"] for o in occs if not o["passed"] and o["lane"] == lane), None) if fresh else None
        if k:
            out[k].append(stamp(b, index))
            covered.update(b.get("item_ids", []))
    last_passed: dict[str, datetime] = {}
    for o in occs:
        if o["passed"]:
            last_passed[o["lane"]] = max(last_passed.get(o["lane"], o["post_at"]), o["post_at"])
    min_eng = int(config.settings().get("monitor", {}).get("idea_min_engagement", 100))
    for c in stories:
        if c["status"] in ("hidden", "used") or c["lead"].id in covered or any(m.id in covered for m in c["members"]):
            continue
        lead = c["lead"]
        if lead.kind == "x_post" and not (lead.meta or {}).get("watchlist"):
            mt = lead.metrics or {}
            if mt.get("like_count", 0) + 2 * mt.get("retweet_count", 0) + 3 * mt.get("quote_count", 0) < min_eng:
                continue  # a random account's post needs real traction to count as an idea (all of them stay in Live)
        lane = config.pillar_lane(c["pillar"])
        target = next((o for o in occs if not o["passed"] and o["lane"] == lane), None)
        if target is None:
            continue
        window = now - timedelta(hours=MIN_WINDOW_HOURS.get(lane, 24))
        since = min(window, last_passed[lane]) if lane in last_passed else window
        if c["newest"] < since:
            continue
        out[target["key"]].append(stamp(from_story(c), index))
    for k, ideas in out.items():
        ideas.sort(key=lambda x: (x["kind"] == "brief", x["hot"], x["score"]), reverse=True)
    return out


SHOWCASE_QUIET_MINUTES = 15  # a run still waiting with no check for this long: the watcher isn't running
LIVE_PHASES = ("waiting", "blocked", "ready")  # still needs you (or the watcher)


def showcase_state(occ: dict, now: datetime | None = None) -> dict | None:
    """Where the Digital Credit Report panel for a showcase slot stands (JSON-safe; the image stays in the DB).

    phase: scheduled (window not open yet) | waiting (the watcher is checking; not ready) | blocked (the audit
    found a problem; still retrying) | ready (image + caption are in the Feed) | posted | missed (deadline passed)."""
    if not occ.get("panel"):
        return None
    now = now or now_ny()
    spec = showcase.panels().get(occ["panel"], {})
    d = date.fromisoformat(occ["date"])
    start, nudge, deadline = (at_ny(d, spec.get(k, dflt)) for k, dflt in
                              (("start", "00:00"), ("nudge", "23:58"), ("deadline", "23:59")))
    out = {"panel": occ["panel"], "title": spec.get("title", occ["panel"].title()), "post": str(spec.get("post", "")),
           "start": start.isoformat(), "nudge": nudge.isoformat(), "deadline": deadline.isoformat(),
           "url": de.report_url(occ["panel"]), "run_id": None, "checked": None, "ready_at": None, "attempts": 0,
           "renders": 0, "blockers": [], "warnings": [], "audit": {}, "png_sha": "", "draft_id": None,
           "phase": "scheduled" if now < start else "missed" if now >= deadline else "waiting", "quiet": False}
    R = db.ShowcaseRun
    with db.session() as s:  # every column but the image
        row = s.execute(select(R.id, R.status, R.updated_at, R.ready_at, R.attempts, R.renders, R.blockers, R.warnings,
                               R.audit_summary, R.png_sha256, R.draft_id)
                        .where(R.run_date == occ["date"], R.panel == occ["panel"]).order_by(R.id.desc()).limit(1)).first()
    if row is not None:
        out.update(run_id=row.id, phase=row.status or "waiting", checked=aware(row.updated_at).isoformat(),
                   ready_at=aware(row.ready_at).isoformat() if row.ready_at else None, attempts=row.attempts or 0,
                   renders=row.renders or 0, blockers=[b.get("detail", "") for b in row.blockers or [] if b.get("detail")],
                   warnings=[w.get("detail", "") for w in row.warnings or [] if w.get("detail")],
                   audit=row.audit_summary or {}, png_sha=row.png_sha256 or "", draft_id=row.draft_id)
    if out["phase"] in ("waiting", "blocked") and now >= deadline:  # the watcher stopped before it could say so
        out["phase"] = "missed"
    if out["phase"] in ("waiting", "blocked") and start <= now < deadline:
        checked = parse_iso(out["checked"])
        out["quiet"] = (checked is None and now - start > timedelta(minutes=SHOWCASE_QUIET_MINUTES)) or (
            checked is not None and now - checked > timedelta(minutes=SHOWCASE_QUIET_MINUTES))
    return out


def showcase_png(run_id: int) -> bytes | None:
    """The run's latest render (the post image once it's ready)."""
    with db.session() as s:
        return s.scalar(select(db.ShowcaseRun.png).where(db.ShowcaseRun.id == run_id))


def parse_at(x: dict) -> datetime | None:
    return parse_iso(x.get("at")) if x.get("at") else None
