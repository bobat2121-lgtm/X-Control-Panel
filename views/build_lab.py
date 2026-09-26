"""🛠 Build Lab: AI-proposed creations and their pipeline, plus the Mon/Wed/Fri showcase lineup (report panels)."""
from __future__ import annotations

import streamlit as st
from sqlalchemy import select

from panel.common import badge, card_key, enqueue, esc_md, hero, is_owner, pillar_badge, section
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


def _launch_text(idea: db.BuildIdea) -> str:
    text = idea.launch_post or f"{idea.hook or idea.title}"
    link = idea.shipped_url or idea.media_url
    return f"{text}\n\n{link}" if link and link not in text else text


def _launch_draft(idea_id: int, date_str: str, slot: str) -> None:
    """A build's launch post goes to the Feed as a regular draft (showcase slots are the report panels)."""
    with db.session() as s:
        idea = s.get(db.BuildIdea, idea_id)
        d = db.Draft(slot=slot, slot_date=date_str, kind="regular", lane=idea.lane, pillar=idea.pillar,
                     tone="analytical", status="new", score=50.0, title=f"Build launch: {idea.title}",
                     build_idea_id=idea.id, chart_hint="none",
                     inspiration=[{"label": "Build", "url": idea.shipped_url or idea.media_url or "",
                                   "author": "Build Lab", "text": idea.hook or idea.title}])
        s.add(d)
        s.flush()
        db.add_variant_version(s, d.id, "A", [_launch_text(idea)], "punchy", "tool:build_lab")
        s.commit()
    st.toast("Launch draft added to the Feed", icon="🛠")


@st.dialog("Build idea", width="large")
def idea_dialog(idea_id: int) -> None:
    with db.session() as s:
        i = s.get(db.BuildIdea, idea_id)
    st.markdown(" ".join([pillar_badge(i.pillar), badge(FORMATS.get(i.format, i.format), "ink"),
                          badge(f"effort {i.effort}", "paper"), badge(STATUS_LABELS.get(i.status, i.status), "paper")]),
                unsafe_allow_html=True)
    st.markdown(f"### {esc_md(i.title)}")
    if i.hook:
        st.markdown(f"> {esc_md(i.hook)}")

    owner = is_owner()
    tabs = st.tabs(["🧩 Build prompt", "📝 Details", "🚀 Launch kit"] + (["✏️ Edit"] if owner else []))
    with tabs[0]:
        st.caption("Paste into Claude Code or Codex to build it:")
        st.code(i.build_prompt or "(no prompt yet)", language=None, wrap_lines=True)
        c = st.columns(4) if owner else []
        for col, (mode, label) in zip(c, [("remix", "🔀 Remix"), ("simpler", "🪶 Simpler"), ("wilder", "🌶 Wilder"),
                                          ("series", "🔁 Make a series")]):
            if col.button(label, key=f"rmx_{mode}_{i.id}", width="stretch"):
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
        if i.launch_post and owner:
            st.link_button("🚀 Post launch on X", xtext.intent_post(_launch_text(i)))
        for n, f in enumerate(i.followups or [], 1):
            st.markdown(f"**Follow-up {n}**")
            st.code(f, language=None, wrap_lines=True)
        if owner:
            slots = config.settings()["slots"]
            c = st.columns([1.2, 1.2, 1])
            d = c[0].date_input("Post date", value=today_ny(), key=f"scd_{i.id}")
            sl = c[1].selectbox("Slot", list(slots), key=f"scs_{i.id}",
                                format_func=lambda k: slots.get(k, {}).get("label", k))
            c[2].button("Add launch draft", key=f"scn_{i.id}", on_click=_launch_draft, args=(i.id, d.isoformat(), sl),
                        type="primary", help="Puts the launch post in the Feed for that slot")
    if owner:
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

live = [i for i in ideas if i.status != "archived"]
_count = {k: sum(1 for i in ideas if i.status == k) for k in STATUSES}
hero("BUILDLAB.EXE", "Ideas worth <em>building</em>.",
     "Creations the Strategist proposes from what's surfacing and what performs, each with a build prompt you can "
     "paste into Claude Code or Codex.",
     stats=[(_count["inbox"], "💡 inbox"), (_count["shortlist"], "⭐ shortlist"),
            (_count["building"], "🔨 building", _count["building"] > 0), (_count["shipped"], "🚢 shipped")],
     kicker=f"{len(live)} live ideas · showcase panels Mon / Wed / Fri", icon="🛠")

# --- showcase lineup (fixed: the Digital Credit Report panels, audited before each post)
section("Showcase lineup", "your Digital Credit Report panels")
st.caption("Mon, Wed and Fri post the panels from digital-credit-report.streamlit.app once the showcase watcher has "
           "audited them (Control Room → Showcase). Build Lab ideas go out in regular slots, or upgrade those panels.")
RUN_ICONS = {"waiting": "⏳", "blocked": "⚠️", "ready": "🟢", "posted": "✅", "missed": "🔴"}
for row in showcase.lineup(14):
    run = row["run"]
    c = st.columns([1.3, 2.2, 2.6, 1.6])
    c[0].markdown(f"**{row['date']:%a %b %d}**")
    c[1].markdown(f"🛠 {row['title']}")
    c[2].markdown(f"{row['label']} · post {row['post']}")
    c[3].markdown(f"{RUN_ICONS.get(run.status, '')} {run.status}" if run else "🗓 scheduled")

# --- picks
section("This week's picks", "highest pick score")
picks = sorted([i for i in live if i.status in ("inbox", "shortlist")], key=pick_score, reverse=True)[:3]
pc = st.columns(3)
for col, i in zip(pc, picks):
    with col.container(border=True, key=card_key("hot", f"pick_{i.id}")):
        st.markdown(f"**{esc_md(i.title)}**")
        st.caption(f"{FORMATS.get(i.format, i.format)} · effort {i.effort} · pick score {pick_score(i)}")
        st.markdown(esc_md(i.hook[:160]))
        b = st.columns(2)
        if b[0].button("Open", key=f"pk_open_{i.id}", width="stretch"):
            idea_dialog(i.id)
        b[1].button("⭐ Shortlist", key=f"pk_sl_{i.id}", on_click=_move, args=(i.id, "shortlist"),
                    width="stretch", disabled=i.status == "shortlist" or not is_owner())
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
if is_owner():
    with ctl[4].popover("🧠 Generate ideas", width="stretch"):
        focus = st.text_input("Focus (optional)", placeholder="e.g. STRC dividend mechanics, robotaxi expansion")
        if st.button("Generate 3 ideas", type="primary"):
            enqueue("ideas_generate", {"focus": focus, "n": 3})
    with ctl[5].popover("➕ New idea", width="stretch"):
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
            with col.container(border=True, key=card_key("card", f"idea_{i.id}")):
                st.markdown(f"**{esc_md(i.title[:70])}**")
                meta = f"{FORMATS.get(i.format, i.format)} · {i.effort} · ⚡{pick_score(i)}"
                if i.showcase_date:
                    meta += f" · 🗓 {i.showcase_date[5:]}"
                if i.expires_on and not i.evergreen:
                    meta += f" · ⏳ {i.expires_on[5:]}"
                st.caption(meta)
                b = st.columns([1, 1, 1])
                if b[0].button("🔍", key=f"op_{i.id}", width="stretch", help="Open"):
                    idea_dialog(i.id)
                idx = STATUSES.index(stt)
                if idx > 0 and is_owner():
                    b[1].button("◀", key=f"bk_{i.id}", on_click=_move, args=(i.id, STATUSES[idx - 1]),
                                width="stretch")
                if idx < len(STATUSES) - 1 and is_owner():
                    b[2].button("▶", key=f"fw_{i.id}", on_click=_move, args=(i.id, STATUSES[idx + 1]),
                                width="stretch")
else:
    rows = [{"id": i.id, "title": i.title, "status": i.status, "format": FORMATS.get(i.format, i.format),
             "pillar": i.pillar, "effort": i.effort, "impact": i.impact, "novelty": i.novelty,
             "timely": i.timeliness, "pick": pick_score(i), "showcase": i.showcase_date or "",
             "series": i.series, "expires": i.expires_on or ""} for i in shown]
    ev = st.dataframe(rows, hide_index=True, width="stretch", on_select="rerun",
                      selection_mode="single-row", key="ideas_table")
    sel = ev.selection.rows if ev and ev.selection else []
    if sel:
        idea_dialog(rows[sel[0]]["id"])

series = sorted({i.series for i in ideas if i.series})
if series:
    section("Series")
    for name in series:
        members = [i for i in ideas if i.series == name]
        st.markdown(f"**{esc_md(name)}**: " + ", ".join(f"{esc_md(i.title)} ({i.status})" for i in members))
