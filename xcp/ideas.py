"""Post ideas, grouped under the slot times you post at (the Monitor page's idea feed).

An idea is either a desk brief (researched at the slot's run time) or a live story from the monitor. Briefs sit
under the slot they were written for. A live story goes to the next upcoming slot in its lane (the AI lane goes to
the AI slot, everything else to the next BTC slot), and drops out once a brief already covers it.
Ideas are plain dicts (JSON-safe) so the Writer tab can pin them.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from xcp import config, db, showcase
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


def assign(stories: list[dict], briefs: list[dict], occs: list[dict], now: datetime | None = None) -> dict[str, list]:
    """occurrence key -> ideas, best first (desk picks, then priority stories, then by score)."""
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
            out[k].append(b)
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
        out[target["key"]].append(from_story(c))
    for k, ideas in out.items():
        ideas.sort(key=lambda x: (x["kind"] == "brief", x["hot"], x["score"]), reverse=True)
    return out


def showcase_status(occ: dict) -> str | None:
    if not occ.get("panel"):
        return None
    with db.session() as s:
        run = showcase.run_for(s, occ["date"], occ["panel"])
        return run.status if run else "waiting"


def parse_at(x: dict) -> datetime | None:
    return parse_iso(x.get("at")) if x.get("at") else None
