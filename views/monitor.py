"""📡 Monitor: what's surfacing right now (your watchlist on X, news, regulators, SEC filings) and desk briefs.

You write the posts; this page hands you the news, the numbers and the angles, newest first.
"""
from __future__ import annotations

from datetime import timedelta

import streamlit as st
from sqlalchemy import select

from panel.common import badge, card_key, esc_html, esc_md, hero, is_owner, pillar_badge, section
from xcp import config, db, gh, xtext
from xcp.agents import monitor
from xcp.timeutil import fmt_ago, fmt_ny, parse_iso, today_ny, utcnow

PILLARS = config.pillars()
LABEL = {k: v.get("label", k) for k, v in PILLARS.items()}
KIND_LABEL = {"news": "News", "official": "Official", "filing": "SEC filings", "x_post": "X"}
owner = is_owner()


# ------------------------------------------------------------------ actions

def _save_to_feed(ctx: dict, text: str) -> None:
    with db.session() as s:
        d = db.Draft(slot="on_demand", slot_date=today_ny().isoformat(), kind="regular", status="edited",
                     pillar=ctx.get("pillar") or "bitcoin", lane=config.pillar_lane(ctx.get("pillar")), tone="timely",
                     title=ctx["title"][:200], numbers=ctx.get("numbers") or [],
                     inspiration=[{"label": x.get("kind", "news"), "url": x.get("url", ""), "author": x.get("publisher", ""),
                                   "text": x.get("title", "")} for x in ctx.get("sources", [])[:6]])
        s.add(d)
        s.flush()
        db.add_variant_version(s, d.id, "A", xtext.split_parts(text), "mine", "me")
        if ctx.get("brief_id"):
            b = s.get(db.Brief, ctx["brief_id"])
            b.status, b.draft_id = "used", d.id
        s.commit()
    if ctx.get("story_key"):
        monitor.set_status(ctx["story_key"], "used")
    st.toast("Saved to the Feed. Post it from there or straight from X.", icon="✍️")


@st.dialog("✍️ Write your post", width="large")
def write_dialog(ctx: dict) -> None:
    st.markdown(f"**{esc_md(ctx['title'])}**")
    if ctx.get("what"):
        st.markdown(esc_md(ctx["what"]))
    if ctx.get("why"):
        st.caption(f"Why it matters: {ctx['why']}")
    if ctx.get("numbers"):
        st.dataframe(ctx["numbers"], hide_index=True, width="stretch")
    if ctx.get("angles"):
        st.markdown("**Angles to think about**\n" + "\n".join(f"- {esc_md(a)}" for a in ctx["angles"]))
    for x in ctx.get("sources", [])[:6]:
        st.markdown(f"- [{esc_md(x.get('publisher', ''))}: {esc_md(x.get('title', '')[:100])}]({x.get('url', '')})")
    text = st.text_area("Your post", key=f"wr_{ctx['key']}", height=170,
                        placeholder="Your take, in your words. Separate thread posts with a line containing only ---")
    n = xtext.weighted_len(xtext.split_parts(text)[0]) if text.strip() else 0
    st.caption(f"{n} characters" + (" · Premium long post: the hook must land in the first 280" if n > 280 else ""))
    c = st.columns(2)
    first = xtext.split_parts(text)[0] if text.strip() else ""
    link = xtext.intent_quote(ctx["quote_url"], first) if ctx.get("quote_url") else xtext.intent_post(first)
    c[0].link_button("🚀 Open in X" + (" (quote post)" if ctx.get("quote_url") else ""), link, type="primary",
                     width="stretch", disabled=not first)
    if c[1].button("💾 Save to the Feed", width="stretch", disabled=not first):
        _save_to_feed(ctx, text)
        st.rerun()


def _story_ctx(c: dict) -> dict:
    lead = c["lead"]
    return {"key": f"s_{c['key']}", "story_key": c["key"], "title": c["title"], "pillar": c["pillar"],
            "what": "" if lead.kind == "x_post" else " ".join((lead.text or "").split("\n", 1)[-1].split())[:500],
            "quote_url": lead.url if lead.kind == "x_post" else None,
            "sources": [{"title": monitor.title_of(m), "url": m.url, "kind": m.kind,
                         "publisher": f"@{m.author}" if m.kind == "x_post" else (m.author or m.source)}
                        for m in c["members"]]}


def _brief_ctx(b: db.Brief) -> dict:
    return {"key": f"b_{b.id}", "brief_id": b.id, "title": b.title, "what": b.what, "why": b.why,
            "numbers": b.numbers, "angles": b.angles, "pillar": b.pillar, "sources": b.sources,
            "quote_url": next((x["url"] for x in b.sources if x.get("kind") == "x_post"), None)
            if b.kind == "reply_target" else None}


def _brief_status(bid: int, status: str) -> None:
    with db.session() as s:
        s.get(db.Brief, bid).status = status
        s.commit()


# ------------------------------------------------------------------ cards

def _actions(c: dict, where: str) -> None:
    if st.button("✍️ Write", key=f"w_{where}_{c['key']}", type="primary"):
        write_dialog(_story_ctx(c))
    if st.button("⭐", key=f"sv_{where}_{c['key']}", help="Save for later"):
        monitor.set_status(c["key"], "" if c["status"] == "saved" else "saved")
        st.rerun()
    if st.button("🙈", key=f"hd_{where}_{c['key']}", help="Hide this story"):
        monitor.set_status(c["key"], "hidden")
        st.rerun()


def story_card(c: dict, compact: bool = False, where: str = "live") -> None:
    lead = c["lead"]
    published = lead.created_at or lead.fetched_at
    new = bool(c["first_seen"] and utcnow() - c["first_seen"] < timedelta(minutes=30)
               and utcnow() - (published if published.tzinfo else published.replace(tzinfo=c["first_seen"].tzinfo))
               < timedelta(hours=3))
    with st.container(border=True, key=card_key("hot" if c["priority"] else "card", f"{where}_{c['key']}")):
        body, side = (st.container(), None) if compact or not owner else st.columns([7, 1.9], vertical_alignment="center")
        with body:
            chips = [pillar_badge(c["pillar"])]
            if c["priority"]:
                chips.append(badge("⚡ " + c["priority"][:40], "hot"))
            if new:
                chips.append(badge("NEW!", "new"))
            if (lead.meta or {}).get("watchlist"):
                chips.append(badge("🎙 your 7", "ink"))
            if c["status"] in ("saved", "used"):
                chips.append(badge("⭐ saved" if c["status"] == "saved" else "✅ used", "paper"))
            who = f"@{lead.author}" if lead.kind == "x_post" else (lead.author or lead.source)
            more = f" · +{c['publishers'] - 1} outlets" if c["publishers"] > 1 else ""
            chips.append(f"<span class='xcp-muted'>{esc_html(who)} · {fmt_ago(published)}{more}</span>")
            st.markdown(" ".join(chips), unsafe_allow_html=True)
            if lead.kind == "x_post":
                mt = lead.metrics or {}
                st.markdown(esc_md(" ".join((lead.text or "").split())[:500 if not compact else 200]))
                st.caption(f"♥ {mt.get('like_count', 0):,} · 🔁 {mt.get('retweet_count', 0):,} · "
                           f"💬 {mt.get('reply_count', 0):,} · [open on X ↗]({lead.url})")
            else:
                st.markdown(f"**[{esc_md(c['title'][:160])}]({lead.url})**")
                if not compact and "\n" in (lead.text or ""):
                    snippet = " ".join((lead.text or "").split("\n", 1)[-1].split())[:240]
                    if snippet:
                        st.caption(snippet)
            if c["publishers"] > 1 and not compact:
                with st.expander(f"{c['publishers']} outlets on this"):
                    for m in c["members"]:
                        st.markdown(f"- [{esc_md(m.author or m.source)}: {esc_md(monitor.title_of(m)[:110])}]({m.url}) "
                                    f"<span class='xcp-muted'>{fmt_ago(m.created_at or m.fetched_at)}</span>",
                                    unsafe_allow_html=True)
        if owner:
            with (side if side is not None else st.container()):
                with st.container(horizontal=True, horizontal_alignment="right" if side is not None else "left",
                                  gap="small"):
                    _actions(c, where)


def brief_card(b: db.Brief) -> None:
    with st.container(border=True, key=card_key("hot" if b.priority >= 3 else "card", f"brief_{b.id}")):
        chips = [pillar_badge(b.pillar), badge({3: "⚡ post now", 2: "today", 1: "worth knowing"}.get(b.priority, ""),
                                               {3: "hot", 2: "ink"}.get(b.priority, "paper"))]
        if b.status in ("saved", "used"):
            chips.append(badge("⭐ saved" if b.status == "saved" else "✅ used", "paper"))
        chips.append(f"<span class='xcp-muted'>{esc_html(b.run_slot)} desk · {fmt_ago(b.created_at)}</span>")
        st.markdown(" ".join(chips), unsafe_allow_html=True)
        st.markdown(f"**{esc_md(b.title)}**")
        if b.what:
            st.markdown(esc_md(b.what))
        if b.why:
            st.caption(f"Why it matters: {b.why}")
        if b.numbers:
            st.caption(" · ".join(f"{x['label']}: {x['value']}" for x in b.numbers[:6]))
        if b.angles:
            st.markdown("\n".join(f"- _{esc_md(a)}_" for a in b.angles))
        if b.flags:
            st.caption("⚠️ " + "; ".join(b.flags))
        if b.sources:
            st.caption(" · ".join(f"[{x.get('publisher', 'source')}]({x.get('url', '')})" for x in b.sources[:5]))
        if owner:
            c = st.columns([1.2, 1, 1, 3])
            if c[0].button("✍️ Write", key=f"bw_{b.id}", width="stretch"):
                write_dialog(_brief_ctx(b))
            if c[1].button("⭐", key=f"bs_{b.id}", help="Save for later", width="stretch"):
                _brief_status(b.id, "new" if b.status == "saved" else "saved")
                st.rerun()
            if c[2].button("🙈", key=f"bh_{b.id}", help="Dismiss", width="stretch"):
                _brief_status(b.id, "dismissed")
                st.rerun()


# ------------------------------------------------------------------ page

def _check_now() -> None:
    with st.spinner("Checking every news source…"):
        news = monitor.ingest_news()
        monitor.cluster_recent()
        monitor.evaluate(news["new_ids"], ping=False)
    ok = gh.can_dispatch() and gh.dispatch("monitor.yml")[0]  # the cloud run adds X and SEC filings
    st.toast(f"{len(news['new_ids'])} new stories from the news feeds"
             + (" · X and SEC filings arrive in about a minute" if ok else ""), icon="📡")


last = db.kv_get("monitor:last_run") or {}
_now12 = monitor.stream(hours=12)
_watch24 = monitor.stream(hours=24, kinds=("x_post",), watchlist_only=True, include_offtopic=True)
with db.session() as _s:
    _briefs_today = _s.query(db.Brief).filter(db.Brief.run_date == today_ny().isoformat(),
                                              db.Brief.kind == "story").count()
_pri = sum(1 for c in _now12 if c["priority"] and c["status"] != "hidden")
_latest = sorted(_now12, key=lambda c: c["newest"], reverse=True)[:14]
_n_sources = len(monitor.feeds()) + len(monitor.watchlist_handles()) + 2
hero("MONITOR.EXE", "What's surfacing <em>right now</em>.",
     "Your 7 accounts on X, the crypto outlets, regulators, Google News topics and SEC filings. Newest first. "
     "You write the posts.",
     stats=[(_pri, "⚡ priority now", _pri > 0), (len(_now12), "stories · 12h"),
            (len(_watch24), "🎙 your 7 · 24h"), (_briefs_today, "desk briefs today")],
     kicker=(f"live wire · checked {fmt_ago(parse_iso(last['at']))} · {_n_sources} sources" if last.get("at")
             else f"live wire · {_n_sources} sources"),
     ticker=[("NEW:" if c["first_seen"] and utcnow() - c["first_seen"] < timedelta(minutes=30) else "")
             + c["title"][:110] for c in _latest], icon="📡")
if owner:
    tb = st.columns([5, 1.3])
    tb[0].caption(f"Last check: {last.get('news_new', 0)} new stories · {last.get('x_new', 0)} new posts from your "
                  f"accounts · {last.get('priority', 0)} flagged" if last.get("at") else "The monitor hasn't run yet.")
    tb[1].button("🔄 Check now", on_click=_check_now, width="stretch", type="primary",
                 help="Pulls the news feeds right away; X and SEC filings follow from the cloud")

tab_live, tab_briefs, tab_watch, tab_saved = st.tabs(["🗞 Live", "🧠 Desk briefs", "🎙 Your 7", "⭐ Saved"])

with tab_live:
    f = st.columns([3, 2.2, 1.6])
    topics = f[0].pills("Topics", list(PILLARS), format_func=LABEL.get, selection_mode="multi", key="mon_topics")
    hours = f[1].segmented_control("Window", [1, 3, 6, 12, 24, 48], default=12, format_func=lambda h: f"{h}h",
                                   key="mon_hours") or 12
    sort = f[2].segmented_control("Sort", ["Newest", "Top now"], default="Newest", key="mon_sort") or "Newest"
    g = st.columns([3, 2.2, 1.6])
    kinds = g[0].pills("Sources", list(KIND_LABEL), format_func=KIND_LABEL.get, selection_mode="multi",
                       default=list(KIND_LABEL), key="mon_kinds")
    query = g[1].text_input("Search", key="mon_q", placeholder="Search titles…", label_visibility="collapsed")
    offlane = g[2].toggle("Off-lane too", key="mon_off", help="Show stories outside your lanes (altcoins, etc.)")

    @st.fragment(run_every="60s")
    def live() -> None:
        stories = monitor.stream(hours=int(hours), include_offtopic=offlane)
        pri = [c for c in stories if c["priority"] and c["status"] != "hidden"]
        if pri:
            section("Priority", "pulsing = worth posting about now")
            cols = st.columns(min(3, len(pri)))
            for i, c in enumerate(sorted(pri, key=lambda c: c["newest"], reverse=True)[:3]):
                with cols[i]:
                    story_card(c, compact=True, where="pri")
        want = set(kinds or KIND_LABEL)
        out = []
        for c in stories:
            lead = c["lead"]
            kind = "official" if (lead.meta or {}).get("official") else lead.kind
            if c["status"] == "hidden" or kind not in want or (topics and c["pillar"] not in topics):
                continue
            if query and query.lower() not in (c["title"] + " " + (lead.text or "")).lower():
                continue
            out.append(c)
        out.sort(key=(lambda c: c["newest"]) if sort == "Newest" else (lambda c: c["score"]), reverse=True)
        st.caption(f"{len(out)} stories · refreshed {fmt_ny(utcnow(), '%I:%M %p')} (updates every minute)")
        for c in out[:60]:
            story_card(c)
        if not out:
            st.info("Nothing in this window yet. The monitor checks every 15 minutes, or press 🔄 Check now.", icon="📡")

    live()

with tab_briefs:
    since = utcnow() - timedelta(hours=36)
    with db.session() as s:
        briefs = list(s.scalars(select(db.Brief).where(db.Brief.created_at >= since, db.Brief.status != "dismissed")
                                .order_by(db.Brief.created_at.desc())).all())
    stories_b = [b for b in briefs if b.kind == "story"]
    replies_b = [b for b in briefs if b.kind == "reply_target"]
    if not stories_b:
        st.info("No desk briefs yet. The desk writes them at your slot times (7:05, 11:20, 12:50 and Friday 16:10 "
                "ET), or run a slot from Control Room.", icon="🧠")
    for b in sorted(stories_b, key=lambda b: (b.created_at.date(), b.priority, b.created_at), reverse=True):
        brief_card(b)
    if replies_b:
        section("Worth replying to", "from the desk")
        for b in replies_b[:6]:
            with st.container(border=True, key=card_key("card", f"reply_{b.id}")):
                st.markdown(esc_md(b.title))
                st.caption(f"Why: {b.why}")
                url = next((x["url"] for x in b.sources if x.get("url")), "")
                if owner and url:
                    tid = xtext.tweet_id_from_url(url)
                    c = st.columns([1.3, 1.3, 3])
                    if tid:
                        c[0].link_button("↩️ Reply on X", xtext.intent_reply(tid, ""), width="stretch")
                    c[1].link_button("Open post ↗", url, width="stretch")

with tab_watch:
    handles = sorted({a["handle"].lstrip("@") for a in config.get("watchlist").get("accounts", []) if a.get("handle")})
    who = st.pills("Accounts", handles, selection_mode="multi", key="mon_who")

    @st.fragment(run_every="60s")
    def watch() -> None:
        posts = monitor.stream(hours=48, kinds=("x_post",), watchlist_only=True, include_offtopic=True)
        if who:
            wanted = {h.lower() for h in who}
            posts = [c for c in posts if (c["lead"].author or "").lower() in wanted]
        posts = [c for c in posts if c["status"] != "hidden"]
        posts.sort(key=lambda c: c["newest"], reverse=True)
        st.caption(f"{len(posts)} posts in the last 48 hours · checked every 15 minutes")
        for c in posts[:60]:
            story_card(c, where="watch")
        if not posts:
            st.info("No posts from your accounts in the last 48 hours yet.", icon="🎙")

    watch()

with tab_saved:
    saved = [c for c in monitor.stream(hours=24 * 7, include_offtopic=True) if c["status"] == "saved"]
    with db.session() as s:
        saved_b = list(s.scalars(select(db.Brief).where(db.Brief.status == "saved").order_by(db.Brief.created_at.desc())
                                 .limit(40)).all())
    if not saved and not saved_b:
        st.info("Nothing saved yet. Press ⭐ on any story or brief.", icon="⭐")
    for b in saved_b:
        brief_card(b)
    for c in sorted(saved, key=lambda c: c["newest"], reverse=True):
        story_card(c, where="saved")
