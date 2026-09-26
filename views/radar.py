"""📡 Radar: stories the Scout found, reply opportunities, and raw signals."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st
from sqlalchemy import select

from panel.common import badge, enqueue, esc_html, esc_md, is_owner, pillar_badge
from xcp import config, db, xtext
from xcp.timeutil import fmt_ago, today_ny, utcnow


def _story_status(story_id: int, status: str) -> None:
    with db.session() as s:
        s.get(db.Story, story_id).status = status
        s.commit()


def _draft_status(draft_id: int, status: str) -> None:
    with db.session() as s:
        s.get(db.Draft, draft_id).status = status
        s.commit()


tab_stories, tab_replies, tab_raw = st.tabs(["🧭 Stories", "↩️ Reply radar", "🗂 Raw signals"])

# ------------------------------------------------------------------ stories
with tab_stories:
    c = st.columns([1.2, 2, 3])
    day = c[0].date_input("Day", value=today_ny(), key="radar_day")
    only_open = c[1].toggle("Hide used/dismissed", value=True)
    if is_owner():
        with c[2].popover("✍️ Draft from a link or idea", width="stretch"):
            src_in = st.text_input("X post URL, article link, or a raw idea", key="rdr_src")
            angle = st.text_input("Your angle (optional)", key="rdr_angle")
            if st.button("Create drafts", type="primary", key="rdr_go") and src_in.strip():
                kind = "draft_from_url" if src_in.strip().startswith("http") else "draft_from_text"
                enqueue(kind, {"url": src_in.strip(), "text": src_in.strip(), "angle": angle})

    with db.session() as s:
        stories = list(s.scalars(select(db.Story).where(db.Story.slot_date == day.isoformat())
                                 .order_by(db.Story.score.desc())).all())
    if only_open:
        stories = [x for x in stories if x.status in ("new", "starred")]
    if not stories:
        st.info("No stories for this day yet. The Scout clusters signals at every slot run.", icon="📡")
    for story in stories:
        with st.container(border=True):
            sc = story.scores or {}
            st.markdown(" ".join([pillar_badge(story.pillar), badge(f"score {story.score:.1f}", "#14171A"),
                                  badge(story.slot, "#8899A6")] + ([badge("⭐ starred", "#E8A33D")]
                                                                   if story.status == "starred" else [])),
                        unsafe_allow_html=True)
            st.markdown(f"**{esc_md(story.title)}**")
            st.markdown(esc_md(story.summary))
            st.caption(" · ".join(f"{k} {v}" for k, v in sc.items()))
            if story.angle_ideas:
                st.markdown("Angles: " + " · ".join(f"_{esc_md(a)}_" for a in story.angle_ideas[:4]))
            if is_owner():
                b = st.columns(4)
                b[0].button("⭐ Star", key=f"st_{story.id}", on_click=_story_status, args=(story.id, "starred"),
                            width="stretch")
                b[1].button("✍️ Draft this", key=f"dr_{story.id}", on_click=enqueue,
                            args=("draft_from_story", {"story_id": story.id}), width="stretch", type="primary")
                b[2].button("🛠 Build idea", key=f"bi_{story.id}", on_click=enqueue,
                            args=("idea_from_story", {"story_id": story.id}), width="stretch")
                b[3].button("🗑 Dismiss", key=f"sd_{story.id}", on_click=_story_status, args=(story.id, "dismissed"),
                            width="stretch")
            with st.expander(f"{len(story.item_ids or [])} source items"):
                with db.session() as s:
                    items = [s.get(db.Item, i) for i in story.item_ids or []]
                for it in [i for i in items if i]:
                    m = it.metrics or {}
                    eng = f" · ♥ {m.get('like_count', 0):,} · 🔁 {m.get('retweet_count', 0):,}" if m else ""
                    who = f"@{it.author}" if it.kind == "x_post" else it.author
                    st.markdown(f'<div class="xcp-src"><b>{esc_html(who)}</b> <span class="xcp-muted">{it.kind} · '
                                f'{fmt_ago(it.created_at)}{eng}</span><br>{esc_html(it.text[:500])}</div>',
                                unsafe_allow_html=True)
                    if it.url:
                        st.markdown(f"[Open ↗]({it.url})")

# ------------------------------------------------------------------ replies
with tab_replies:
    st.caption("Fast-rising posts from big accounts where an early, sharp reply earns reach.")
    since = (today_ny() - timedelta(days=1)).isoformat()
    with db.session() as s:
        replies = list(s.scalars(select(db.Draft).where(db.Draft.kind == "reply", db.Draft.slot_date >= since,
                                                        db.Draft.status.in_(["new", "edited"]))
                                 .order_by(db.Draft.score.desc())).all())
        rv = {d.id: db.current_variants(s, d.id) for d in replies}
    if not replies:
        st.info("No reply opportunities right now.", icon="↩️")
    for d in replies:
        src = (d.inspiration or [{}])[0]
        variants = rv.get(d.id) or []
        if not variants:
            continue
        with st.container(border=True):
            m = src.get("metrics") or {}
            st.markdown(f'<div class="xcp-src"><b>@{esc_html(src.get("author", ""))}</b> <span class="xcp-muted">'
                        f'♥ {m.get("like_count", 0):,} · 🔁 {m.get("retweet_count", 0):,} · '
                        f'{(src.get("followers") or 0):,} followers</span><br>{esc_html(src.get("text", "")[:400])}'
                        f'</div>', unsafe_allow_html=True)
            if d.title:
                st.caption(f"Why: {esc_md(d.title)}")
            key = f"rp_{variants[0].id}"
            text = st.text_area("Reply", value=xtext.join_parts(variants[0].parts), key=key, height=90,
                                label_visibility="collapsed", disabled=not is_owner())
            st.caption(f"{xtext.weighted_len(text)} chars")
            if is_owner():
                b = st.columns([1.3, 1, 1, 1])
                tid = xtext.tweet_id_from_url(src.get("url"))
                if tid:
                    b[0].link_button("↩️ Reply on X", xtext.intent_reply(tid, text), type="primary", width="stretch")
                if src.get("url"):
                    b[1].link_button("Open post ↗", src["url"], width="stretch")
                b[2].button("✅ Done", key=f"rpd_{d.id}", on_click=_draft_status, args=(d.id, "posted"),
                            width="stretch")
                b[3].button("🗑 Skip", key=f"rps_{d.id}", on_click=_draft_status, args=(d.id, "dismissed"),
                            width="stretch")

# ------------------------------------------------------------------ raw
with tab_raw:
    c = st.columns([1, 1, 1, 2])
    hours = c[0].selectbox("Window", [6, 12, 24, 48, 96], index=2, format_func=lambda h: f"last {h}h")
    kinds = c[1].multiselect("Kind", ["x_post", "news", "filing"], default=["x_post", "news", "filing"])
    lanes = c[2].multiselect("Lane", ["btc", "ai"], default=["btc", "ai"])
    search = c[3].text_input("Search text", placeholder="e.g. STRC, SWE-bench, Waymo")
    since_dt = utcnow() - timedelta(hours=hours)
    with db.session() as s:
        items = list(s.scalars(select(db.Item).where(db.Item.fetched_at >= since_dt)
                               .order_by(db.Item.score.desc()).limit(800)).all())
    items = [i for i in items if i.kind in kinds and i.lane in lanes
             and (not search or search.lower() in (i.text or "").lower())]
    pl = config.pillars()
    rows = [{"score": i.score, "kind": i.kind, "pillar": pl.get(i.pillar, {}).get("label", i.pillar),
             "author": i.author, "age": fmt_ago(i.created_at or i.fetched_at), "text": (i.text or "")[:220],
             "likes": (i.metrics or {}).get("like_count"), "link": i.url} for i in items]
    st.caption(f"{len(rows)} items")
    st.dataframe(rows, hide_index=True, width="stretch", height=560,
                 column_config={"link": st.column_config.LinkColumn("link", display_text="open ↗"),
                                "text": st.column_config.TextColumn("text", width="large")})
