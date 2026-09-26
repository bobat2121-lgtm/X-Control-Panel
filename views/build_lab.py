"""🛠 Build Lab: AI-proposed creations, pipeline, and the Mon/Wed/Fri showcase lineup."""
from __future__ import annotations

from datetime import date

import streamlit as st
from sqlalchemy import select

from panel.common import badge, enqueue, esc_md, pillar_badge
from xcp import config, db, showcase, xtext
from xcp.timeutil import fmt_ago, today_ny, utcnow

STATUSES = ["inbox", "shortlist", "building", "ready", "shipped", "archived"]
STATUS_LABELS = {"inbox": "💡 Inbox", "shortlist": "⭐ Shortlist", "building": "🔨 Building", "ready": "✅ Ready",
                 "shipped": "🚢 Shipped", "archived": "🗄 Archive"}
FORMATS = {"gif_sim": "🎞 GIF sim", "short_video": "🎬 Short video", "streamlit_page": "🖥 Streamlit page",
           "chart_pack": "📈 Chart pack", "calculator": "🧮 Calculator", "thread_series": "🧵 Thread series",
           "meme_template": "😂 Meme template", "live_tracker": "📡 Live tracker"}
EFFORT_W = {"S": 1.0, "M": 1.6, "L": 2.8}


def pick_score(i: db.BuildIdea) -> float:
    expired = i.expires_on and i.expires_on < today_ny().isoformat()
    t = 0 if expired else i.timeliness
    return round((i.impact + 0.6 * i.novelty + 0.8 * t) / EFFORT_W.get(i.effort, 1.6), 1)


def _move(idea_id: int, status: str) -> None:
    with db.session() as s:
        i = s.get(db.BuildIdea, idea_id)
        i.status, i.updated_at = status, utcnow()
        s.commit()


def _assign(date_str: str, slot: str, key: str) -> None:
    idea_id = st.session_state.get(key)
    with db.session() as s:
        for old in s.scalars(select(db.BuildIdea).where(db.BuildIdea.showcase_date == date_str,
                                                        db.BuildIdea.showcase_slot == slot)):
            old.showcase_date, old.showcase_slot = None, None
        if idea_id:
            i = s.get(db.BuildIdea, idea_id)
            i.showcase_date, i.showcase_slot, i.updated_at = date_str, slot, utcnow()
            if i.status == "inbox":
                i.status = "shortlist"
        s.commit()


def _showcase_now(idea_id: int, date_str: str, slot: str) -> None:
    with db.session() as s:
        if showcase.existing_showcase_draft(s, date_str, slot):
            st.toast("A showcase draft already exists for that slot. It's in the Feed.", icon="ℹ️")
            return
        showcase.create_showcase_draft(s, s.get(db.BuildIdea, idea_id), slot, date_str)
        s.commit()
    st.toast("Showcase draft created in the Feed", icon="🛠")


@st.dialog("Build idea", width="large")
def idea_dialog(idea_id: int) -> None:
    with db.session() as s:
        i = s.get(db.BuildIdea, idea_id)
    st.markdown(" ".join([pillar_badge(i.pillar), badge(FORMATS.get(i.format, i.format), "#14171A"),
                          badge(f"effort {i.effort}", "#5B7083"), badge(STATUS_LABELS.get(i.status, i.status), "#8899A6")]),
                unsafe_allow_html=True)
    st.markdown(f"### {esc_md(i.title)}")
    if i.hook:
        st.markdown(f"> {esc_md(i.hook)}")

    tabs = st.tabs(["🧩 Build prompt", "📝 Details", "🚀 Launch kit", "✏️ Edit"])
    with tabs[0]:
        st.caption("Paste into Claude Code or Codex to build it:")
        st.code(i.build_prompt or "(no prompt yet)", language=None, wrap_lines=True)
        c = st.columns(4)
        for col, (mode, label) in zip(c, [("remix", "🔀 Remix"), ("simpler", "🪶 Simpler"), ("wilder", "🌶 Wilder"),
                                          ("series", "🔁 Make a series")]):
            if col.button(label, key=f"rmx_{mode}_{i.id}", use_container_width=True):
                enqueue("idea_remix", {"idea_id": i.id, "mode": mode})
    with tabs[1]:
        st.markdown(f"**Why now:** {esc_md(i.why_now) or '—'}")
        if i.expires_on:
            st.caption(f"⏳ Timely until {i.expires_on}")
        st.markdown(f"**Concept (what the viewer sees)**\n\n{esc_md(i.concept) or '—'}")
        st.markdown(f"**Data inputs**\n\n{esc_md(i.data_inputs) or '—'}")
        st.markdown(f"**Build spec**\n\n{esc_md(i.build_spec) or '—'}")
        for link in i.signal_links or []:
            st.markdown(f"- {esc_md(link)}")
        st.caption(f"Impact {i.impact} · Novelty {i.novelty} · Timeliness {i.timeliness} · Pick score {pick_score(i)}"
                   f" · {'evergreen' if i.evergreen else 'timely'} · created {fmt_ago(i.created_at)} · {esc_md(i.notes)}")
    with tabs[2]:
        st.markdown("**Launch post**")
        st.code(i.launch_post or "—", language=None, wrap_lines=True)
        if i.launch_post:
            st.link_button("🚀 Post launch on X", xtext.intent_post(showcase.default_showcase_text(i)))
        for n, f in enumerate(i.followups or [], 1):
            st.markdown(f"**Follow-up {n}**")
            st.code(f, language=None, wrap_lines=True)
        slots = config.settings()["slots"]
        c = st.columns([1.2, 1.2, 1])
        d = c[0].date_input("Showcase date", value=date.fromisoformat(i.showcase_date) if i.showcase_date else today_ny(),
                            key=f"scd_{i.id}")
        sc_slots = showcase.showcase_slots(d) or list(slots)
        sl = c[1].selectbox("Slot", sc_slots, key=f"scs_{i.id}", format_func=lambda k: slots.get(k, {}).get("label", k))
        c[2].button("Create showcase draft", key=f"scn_{i.id}", on_click=_showcase_now, args=(i.id, d.isoformat(), sl),
                    type="primary", help="Puts the launch post in the Feed for that slot")
    with tabs[3]:
        with st.form(f"edit_{i.id}"):
            title = st.text_input("Title", i.title)
            hook = st.text_input("Hook", i.hook)
            c = st.columns(4)
            status = c[0].selectbox("Status", STATUSES, index=STATUSES.index(i.status), format_func=STATUS_LABELS.get)
            fmt = c[1].selectbox("Format", list(FORMATS), index=list(FORMATS).index(i.format) if i.format in FORMATS else 0,
                                 format_func=FORMATS.get)
            effort = c[2].selectbox("Effort", ["S", "M", "L"], index=["S", "M", "L"].index(i.effort or "M"))
            pillar = c[3].selectbox("Pillar", list(config.pillars()), index=list(config.pillars()).index(i.pillar)
                                    if i.pillar in config.pillars() else 0)
            c2 = st.columns(3)
            impact = c2[0].slider("Impact", 1, 10, i.impact)
            novelty = c2[1].slider("Novelty", 1, 10, i.novelty)
            timeliness = c2[2].slider("Timeliness", 1, 10, i.timeliness)
            shipped_url = st.text_input("Shipped link (Streamlit page / post / repo)", i.shipped_url)
            media_url = st.text_input("Media link (GIF / video)", i.media_url)
            series = st.text_input("Series", i.series)
            launch_post = st.text_area("Launch post", i.launch_post, height=100)
            concept = st.text_area("Concept", i.concept, height=120)
            build_prompt = st.text_area("Build prompt", i.build_prompt, height=200)
            notes = st.text_area("Notes", i.notes, height=80)
            if st.form_submit_button("Save", type="primary"):
                with db.session() as s:
                    row = s.get(db.BuildIdea, i.id)
                    row.title, row.hook, row.status, row.format, row.effort = title, hook, status, fmt, effort
                    row.pillar, row.lane = pillar, config.pillar_lane(pillar)
                    row.impact, row.novelty, row.timeliness = impact, novelty, timeliness
                    row.shipped_url, row.media_url, row.series = shipped_url, media_url, series
                    row.launch_post, row.concept, row.build_prompt, row.notes = launch_post, concept, build_prompt, notes
                    row.updated_at = utcnow()
                    s.commit()
                st.rerun()


# ------------------------------------------------------------------ page

with db.session() as s:
    ideas = list(s.scalars(select(db.BuildIdea).order_by(db.BuildIdea.created_at.desc())).all())
by_id = {i.id: i for i in ideas}
live = [i for i in ideas if i.status != "archived"]

# --- showcase lineup
st.markdown("#### 🗓 Showcase lineup: Mon morning · Wed midday · Fri after close")
lineup = showcase.lineup(14)
opts = [None] + [i.id for i in live]
for row in lineup:
    idea = row["idea"]
    c = st.columns([1.3, 1.5, 3.2, 1.2])
    c[0].markdown(f"**{row['date']:%a %b %d}**")
    c[1].markdown(f"{row['label']} · {row['post_at']}")
    key = f"lu_{row['date']}_{row['slot']}"
    c[2].selectbox("Assigned build", opts, index=opts.index(idea.id) if idea and idea.id in opts else 0, key=key,
                   label_visibility="collapsed", on_change=_assign, args=(row["date"].isoformat(), row["slot"], key),
                   format_func=lambda x: "— pick a build —" if x is None else f"#{x} {by_id[x].title[:60]}")
    if idea is None:
        c[3].markdown("⚠️ empty")
    elif idea.status in showcase.READY:
        c[3].markdown(f"✅ {idea.status}" + (" · 🔗" if idea.shipped_url else " · no link"))
    else:
        c[3].markdown(f"🔨 {idea.status}")

# --- picks
st.markdown("#### 🏆 This week's picks")
picks = sorted([i for i in live if i.status in ("inbox", "shortlist")], key=pick_score, reverse=True)[:3]
pc = st.columns(3)
for col, i in zip(pc, picks):
    with col.container(border=True):
        st.markdown(f"**{esc_md(i.title)}**")
        st.caption(f"{FORMATS.get(i.format, i.format)} · effort {i.effort} · pick score {pick_score(i)}")
        st.markdown(esc_md(i.hook[:160]))
        b = st.columns(2)
        if b[0].button("Open", key=f"pk_open_{i.id}", use_container_width=True):
            idea_dialog(i.id)
        b[1].button("⭐ Shortlist", key=f"pk_sl_{i.id}", on_click=_move, args=(i.id, "shortlist"),
                    use_container_width=True, disabled=i.status == "shortlist")
if not picks:
    st.caption("No ideas in the inbox yet. The Strategist adds 3 every night, or generate some now 👇")

# --- controls
st.divider()
ctl = st.columns([1.2, 1.6, 1.6, 1.2, 1.4, 1.4])
view = ctl[0].segmented_control("View", ["Board", "Table"], default="Board", label_visibility="collapsed") or "Board"
f_fmt = ctl[1].multiselect("Format", list(FORMATS), format_func=FORMATS.get, placeholder="All formats",
                           label_visibility="collapsed")
f_pillar = ctl[2].multiselect("Pillar", list(config.pillars()), placeholder="All pillars", label_visibility="collapsed",
                              format_func=lambda p: config.pillars()[p].get("label", p))
show_arch = ctl[3].toggle("Archive")
with ctl[4].popover("🧠 Generate ideas", use_container_width=True):
    focus = st.text_input("Focus (optional)", placeholder="e.g. STRC dividend mechanics, robotaxi expansion")
    if st.button("Generate 3 ideas", type="primary"):
        enqueue("ideas_generate", {"focus": focus, "n": 3})
with ctl[5].popover("➕ New idea", use_container_width=True):
    with st.form("new_idea", clear_on_submit=True):
        t = st.text_input("Title")
        h = st.text_input("Hook")
        fm = st.selectbox("Format", list(FORMATS), format_func=FORMATS.get)
        pl = st.selectbox("Pillar", list(config.pillars()))
        ef = st.selectbox("Effort", ["S", "M", "L"], index=1)
        cp = st.text_area("Concept")
        if st.form_submit_button("Add") and t:
            with db.session() as s:
                s.add(db.BuildIdea(title=t, hook=h, format=fm, pillar=pl, lane=config.pillar_lane(pl), effort=ef,
                                   concept=cp, source="manual"))
                s.commit()
            st.rerun()

shown = [i for i in ideas if (show_arch or i.status != "archived")
         and (not f_fmt or i.format in f_fmt) and (not f_pillar or i.pillar in f_pillar)]

if view == "Board":
    cols_status = [x for x in STATUSES if show_arch or x != "archived"]
    cols = st.columns(len(cols_status))
    for col, stt in zip(cols, cols_status):
        items = sorted([i for i in shown if i.status == stt], key=pick_score, reverse=True)
        col.markdown(f"**{STATUS_LABELS[stt]}** · {len(items)}")
        for i in items:
            with col.container(border=True):
                st.markdown(f"**{esc_md(i.title[:70])}**")
                meta = f"{FORMATS.get(i.format, i.format)} · {i.effort} · ⚡{pick_score(i)}"
                if i.showcase_date:
                    meta += f" · 🗓 {i.showcase_date[5:]}"
                if i.expires_on and not i.evergreen:
                    meta += f" · ⏳ {i.expires_on[5:]}"
                st.caption(meta)
                b = st.columns([1, 1, 1])
                if b[0].button("🔍", key=f"op_{i.id}", use_container_width=True, help="Open"):
                    idea_dialog(i.id)
                idx = STATUSES.index(stt)
                if idx > 0:
                    b[1].button("◀", key=f"bk_{i.id}", on_click=_move, args=(i.id, STATUSES[idx - 1]),
                                use_container_width=True)
                if idx < len(STATUSES) - 1:
                    b[2].button("▶", key=f"fw_{i.id}", on_click=_move, args=(i.id, STATUSES[idx + 1]),
                                use_container_width=True)
else:
    rows = [{"id": i.id, "title": i.title, "status": i.status, "format": FORMATS.get(i.format, i.format),
             "pillar": i.pillar, "effort": i.effort, "impact": i.impact, "novelty": i.novelty,
             "timely": i.timeliness, "pick": pick_score(i), "showcase": i.showcase_date or "",
             "series": i.series, "expires": i.expires_on or ""} for i in shown]
    ev = st.dataframe(rows, hide_index=True, use_container_width=True, on_select="rerun",
                      selection_mode="single-row", key="ideas_table")
    sel = ev.selection.rows if ev and ev.selection else []
    if sel:
        idea_dialog(rows[sel[0]]["id"])

series = sorted({i.series for i in ideas if i.series})
if series:
    st.markdown("#### 🔁 Series")
    for name in series:
        members = [i for i in ideas if i.series == name]
        st.markdown(f"**{esc_md(name)}**: " + ", ".join(f"{esc_md(i.title)} ({i.status})" for i in members))
