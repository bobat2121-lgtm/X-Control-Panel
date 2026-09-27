"""📰 Feed: X-style draft cards with editing, one-click rewrites and posting."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st
from sqlalchemy import select

from panel import cache
from panel.common import (STATUS_ICONS, badge, card_key, enqueue, esc_html, esc_md, handle, is_owner, page,
                          pillar_badge, section)
from xcp import charts, config, db, gh, showcase, xtext
from xcp.agents import analyst
from xcp.sources import digital_exposure as de
from xcp.timeutil import at_ny, days_match, fmt_ago, fmt_ny, now_ny, today_ny, utcnow

page("FEED")  # this page's tab title and pixel icon (bookmarks pick them up)

settings = config.settings()
SLOTS = settings["slots"]
SLOT_ORDER = list(SLOTS) + ["on_demand"]
TARGET_LABELS = {"digital_credit": "Digital credit", "bitcoin": "Bitcoin", "macro": "Macro", "ai": "AI"}
REWRITES = [("shorter", "Shorter"), ("punchier", "Punchier"), ("more_data", "+ Data"), ("funnier", "Funnier"),
            ("hook", "Hook"), ("thread", "Thread"), ("single", "Single")]
DISMISS_REASONS = ["Off-topic", "Stale / too late", "Not my voice", "Weak angle", "Factually shaky", "Other"]


def slot_label(key: str) -> str:
    return SLOTS.get(key, {}).get("label", "✍️ On demand" if key == "on_demand" else key)


# ------------------------------------------------------------------ data (one batched load, cached; writes bust it)

FEED_TTL = 30  # AI rewrites finishing in the cloud show up within this


def _fetch_day(day_iso: str, bank: bool) -> dict:
    """Drafts for a day (or the evergreen bank) with their options, history, requests, builds and showcase runs:
    five queries in all, instead of four per card."""
    with db.session() as s:
        q = select(db.Draft).where(db.Draft.kind.in_(["regular", "showcase"]))
        q = q.where(db.Draft.status == "banked") if bank else q.where(db.Draft.slot_date == day_iso)
        drafts = list(s.scalars(q).all())
        ids = [d.id for d in drafts]
        variants = list(s.scalars(select(db.Variant).where(db.Variant.draft_id.in_(ids))
                                  .order_by(db.Variant.version.desc())).all()) if ids else []
        pending = db.pending_requests(s)
        idea_ids = [d.build_idea_id for d in drafts if d.build_idea_id]
        ideas = ({i.id: i for i in s.scalars(select(db.BuildIdea).where(db.BuildIdea.id.in_(idea_ids)))}
                 if idea_ids else {})
        runs = ({r.draft_id: r for r in s.scalars(select(db.ShowcaseRun).where(db.ShowcaseRun.draft_id.in_(ids))
                                                  .order_by(db.ShowcaseRun.id))} if ids else {})
    current: dict[int, list] = {}
    history: dict[tuple, list] = {}
    for v in variants:
        if v.is_current:
            current.setdefault(v.draft_id, []).append(v)
        history.setdefault((v.draft_id, v.label), []).append(v)
    for vs in current.values():
        vs.sort(key=lambda v: v.label)
    return {"drafts": drafts, "current": current, "history": history, "pending": pending, "ideas": ideas,
            "runs": runs}


def _day(day_iso: str, bank: bool = False) -> dict:
    return cache.get(("feed", "day", day_iso, bank), lambda: _fetch_day(day_iso, bank), ttl=FEED_TTL)


def _changed() -> None:
    cache.bust("feed", "score")


# ------------------------------------------------------------------ callbacks

def _save_edit(draft_id: int, label: str, style: str, key: str) -> None:
    parts = xtext.split_parts(st.session_state.get(key, ""))
    with db.session() as s:
        cur = s.scalars(select(db.Variant).where(db.Variant.draft_id == draft_id, db.Variant.label == label,
                                                 db.Variant.is_current.is_(True))).first()
        if cur and cur.parts == parts:
            return
        db.add_variant_version(s, draft_id, label, parts, "thread" if len(parts) > 1 else style, "me")
        d = s.get(db.Draft, draft_id)
        if d.status in ("new", "snoozed"):
            d.status = "edited"
        s.commit()
    _changed()
    st.toast("Saved", icon="💾")


def _local_tool(draft_id: int, label: str, style: str, key: str, tool: str) -> None:
    text = st.session_state.get(key, "")
    if tool == "split":
        parts = xtext.split_into_thread(" ".join(xtext.split_parts(text)) if "\n---\n" in text else text)
    else:
        parts = [xtext.strip_decoration(p) for p in xtext.split_parts(text)]
    with db.session() as s:
        db.add_variant_version(s, draft_id, label, parts, "thread" if len(parts) > 1 else style, f"tool:{tool}")
        s.commit()
    _changed()


def _rewrite(draft_id: int, label: str, preset: str, instruction: str = "") -> None:
    enqueue("rewrite", {"draft_id": draft_id, "label": label, "preset": preset, "instruction": instruction})


def _rewrite_custom(draft_id: int, label: str, key: str) -> None:
    instr = (st.session_state.get(key) or "").strip()
    if not instr:
        st.toast("Type an instruction first", icon="✍️")
        return
    _rewrite(draft_id, label, "", instr)
    st.session_state[key] = ""


def _mark_posted(draft_id: int, label: str, text_key: str, url_key: str) -> None:
    text = st.session_state.get(text_key, "")
    url = (st.session_state.get(url_key) or "").strip()
    with db.session() as s:
        d = s.get(db.Draft, draft_id)
        v = s.scalars(select(db.Variant).where(db.Variant.draft_id == draft_id, db.Variant.label == label,
                                               db.Variant.is_current.is_(True))).first()
        d.status, d.posted_url, d.posted_at, d.chosen_label = "posted", url or None, utcnow(), label
        if d.story_id and (story := s.get(db.Story, d.story_id)):
            story.status = "used"
        if d.build_idea_id and (idea := s.get(db.BuildIdea, d.build_idea_id)):
            idea.status, idea.updated_at = "shipped", utcnow()
        if d.kind == "showcase" and (run := showcase.run_for_draft(s, d.id)):
            run.status, run.updated_at = "posted", utcnow()
        s.commit()
        pillar, tone, kind, style = d.pillar, d.tone, d.kind, v.style if v else None
    analyst.log_post(draft_id, url, text.replace(xtext.THREAD_SEP, "\n\n"), pillar, tone, style, kind)
    _changed()
    st.toast("Logged as posted. It counts toward your mix now.", icon="✅")


def _set_status(draft_id: int, status: str, reason_key: str | None = None) -> None:
    with db.session() as s:
        d = s.get(db.Draft, draft_id)
        d.status = status
        if reason_key:
            d.dismiss_reason = st.session_state.get(reason_key)
        s.commit()
    _changed()


def _snooze(draft_id: int) -> None:
    """Move a draft to the next slot on the schedule."""
    with db.session() as s:
        d = s.get(db.Draft, draft_id)
        now = now_ny()
        for offset in range(0, 8):
            day = now.date() + timedelta(days=offset)
            for key, spec in SLOTS.items():
                if key == d.slot and offset == 0:
                    continue
                if days_match(spec.get("days"), day) and at_ny(day, spec["post_at"]) > now:
                    d.slot, d.slot_date, d.status = key, day.isoformat(), "snoozed"
                    s.commit()
                    _changed()
                    st.toast(f"Snoozed to {slot_label(key)} {day:%a}", icon="⏰")
                    return


def _restore(draft_id: int, label: str, parts: list[str], style: str, version: int) -> None:
    with db.session() as s:
        db.add_variant_version(s, draft_id, label, parts, style, f"restore:v{version}")
        s.commit()
    _changed()


# ------------------------------------------------------------------ card

def _height(parts: list[str]) -> int:
    chars = sum(len(p) for p in parts) + 60 * (len(parts) - 1)
    return int(min(420, max(110, 26 * (chars / 70 + len(parts) + 1))))


def _owner_actions(d: db.Draft, v: db.Variant, parts: list[str], text: str, text_key: str) -> None:
    """Posting, status and rewrite controls (owner only)."""
    first = parts[0]
    src = next((i for i in (d.inspiration or []) if xtext.tweet_id_from_url(i.get("url"))), None)
    a = st.columns([1.3, 1, 1, 1, 1, 1])
    a[0].link_button("🚀 Post on X", xtext.intent_post(first), type="primary", width="stretch",
                     help="Opens X's composer with this text" + (" (part 1; post the rest as replies)" if len(parts) > 1 else ""))
    if src:
        a[1].link_button("💬 Quote", xtext.intent_quote(src["url"], first), width="stretch")
        a[2].link_button("↩️ Reply", xtext.intent_reply(xtext.tweet_id_from_url(src["url"]), first),
                         width="stretch")
    with a[3].popover("✅ Posted", width="stretch"):
        st.text_input("Post URL (recommended, links metrics)", key=f"purl_{d.id}",
                      placeholder="https://x.com/you/status/…")
        st.button("Mark as posted", key=f"pbtn_{d.id}", type="primary", on_click=_mark_posted,
                  args=(d.id, v.label, text_key, f"purl_{d.id}"))
    with a[4].popover("🗑 Dismiss", width="stretch"):
        st.radio("Why? (teaches the writer)", DISMISS_REASONS, key=f"dr_{d.id}")
        st.button("Dismiss", key=f"dbtn_{d.id}", on_click=_set_status, args=(d.id, "dismissed", f"dr_{d.id}"))
    with a[5].popover("⋯ More", width="stretch"):
        st.button("⭐ Bank as evergreen", key=f"bank_{d.id}", on_click=_set_status, args=(d.id, "banked"),
                  width="stretch")
        st.button("⏰ Snooze to next slot", key=f"snz_{d.id}", on_click=_snooze, args=(d.id,),
                  width="stretch")
        st.button("🔁 Regenerate all options (AI)", key=f"regen_{d.id}", on_click=enqueue,
                  args=("regenerate", {"draft_id": d.id}), width="stretch")
        if d.status in ("dismissed", "banked", "posted"):
            st.button("↩️ Back to new", key=f"new_{d.id}", on_click=_set_status, args=(d.id, "new"),
                      width="stretch")
        prompt = (f"Give me 3 sharper versions of this X post for @{handle() or 'me'} "
                  f"(Bitcoin/digital credit/AI account). Keep facts; no hashtags.\n\n{text}")
        st.link_button("💬 Open in ChatGPT", xtext.chatgpt_link(prompt), width="stretch")
        st.caption("Copy:")
        st.code(text, language=None, wrap_lines=True)

    r = st.columns(len(REWRITES))
    for i, (preset, lbl) in enumerate(REWRITES):
        r[i].button(lbl, key=f"rw_{preset}_{d.id}", on_click=_rewrite, args=(d.id, v.label, preset),
                    width="stretch", help="AI rewrite of this option (runs on your ChatGPT account)")
    c = st.columns([5, 1.1, 1.1, 1])
    c[0].text_input("Custom rewrite", key=f"ci_{d.id}", label_visibility="collapsed",
                    placeholder="Custom rewrite… e.g. compare STRC's yield to the 10Y")
    c[1].button("✨ Rewrite", key=f"cib_{d.id}", on_click=_rewrite_custom, args=(d.id, v.label, f"ci_{d.id}"),
                width="stretch")
    c[2].button("✂️ Thread-ify", key=f"split_{d.id}", on_click=_local_tool,
                args=(d.id, v.label, v.style, text_key, "split"), help="Split locally, no AI",
                width="stretch")
    c[3].button("🧹 Plain", key=f"plain_{d.id}", on_click=_local_tool,
                args=(d.id, v.label, v.style, text_key, "plain"), help="Strip emoji and hashtags",
                width="stretch")


RUN_ICONS = {"waiting": "⏳", "blocked": "⚠️", "ready": "🟢", "posted": "✅", "missed": "🔴"}


def _showcase_image(run: db.ShowcaseRun) -> None:
    """The audited Digital Credit Report image for a showcase card."""
    n = run.audit_summary or {}
    ours = f"{sum(1 for c in run.checks or [] if c.get('status') == 'PASS')}/{len(run.checks or [])}"
    st.markdown(
        f"{badge(RUN_ICONS.get(run.status, '') + ' ' + run.status.upper(), 'hot' if run.status == 'ready' else 'paper')} "
        f"<span class='xcp-muted'>audited {fmt_ny(run.ready_at or run.updated_at, '%a %I:%M %p')} · digital-exposure "
        f"{n.get('PASS', 0)} PASS · {n.get('WARN', 0)} WARN · {n.get('FAIL', 0)} FAIL · our checks {ours} · "
        f"code {run.de_commit[:7]}{' (last-good fallback)' if run.used_fallback else ''}</span>",
        unsafe_allow_html=True)
    if run.png:
        c = st.columns([3, 1.2])
        c[0].image(run.png, width="stretch")
        c[1].download_button("⬇️ Download image to attach", run.png, file_name=f"{run.panel}-{run.run_date}.png",
                             mime="image/png", key=f"scdl_{run.id}", type="primary", width="stretch")
        c[1].link_button("🌐 Web report", de.report_url(run.panel), width="stretch")
        c[1].caption("X's composer link can't carry images: download it here (or save it from the Discord 🟢 "
                     "message), then attach it to the post.")
    for w in run.warnings or []:
        st.caption(f"⚠️ {esc_md(w.get('detail', ''))}")


def render_draft(d: db.Draft, data: dict) -> None:
    variants = data["current"].get(d.id, [])
    pending = [r for r in data["pending"] if (r.payload or {}).get("draft_id") == d.id]
    idea = data["ideas"].get(d.build_idea_id) if d.build_idea_id else None
    sc_run = data["runs"].get(d.id) if d.kind == "showcase" else None
    if not variants:
        return
    by_label = {v.label: v for v in variants}
    labels = list(by_label)
    flags = d.editor_flags or []

    with st.container(border=True, key=card_key("hot" if d.kind == "showcase" and d.status != "posted" else "card",
                                                f"draft_{d.id}")):
        chips = []
        if d.kind == "showcase":
            chips.append(badge("🛠 SHOWCASE", "hot"))
        chips += [pillar_badge(d.pillar), badge(d.tone, "paper")]
        chips.append(f'<span class="xcp-muted">{STATUS_ICONS.get(d.status, "")} {d.status} · score {d.score:.1f} · '
                     f'{fmt_ago(d.created_at)}</span>')
        st.markdown(" ".join(chips), unsafe_allow_html=True)
        if d.title:
            st.markdown(f"**{esc_md(d.title)}**")
        if pending:
            st.info(f"⏳ {len(pending)} AI request(s) running for this draft. Refresh in a minute.", icon="🛰️")
        if sc_run:
            _showcase_image(sc_run)

        default = d.chosen_label if d.chosen_label in labels else labels[0]
        choice = st.segmented_control("Option", labels, default=default, key=f"opt_{d.id}",
                                      format_func=lambda lb: f"{lb} · {by_label[lb].style}",
                                      label_visibility="collapsed") or default
        v = by_label[choice]
        text_key = f"ta_{v.id}"
        text = st.text_area("Post", value=xtext.join_parts(v.parts), key=text_key, height=_height(v.parts),
                            label_visibility="collapsed", on_change=_save_edit, args=(d.id, v.label, v.style, text_key),
                            disabled=not is_owner(),
                            help="Edits save automatically (Ctrl+Enter or click away). "
                                 "Separate thread posts with a line containing only ---")
        parts = xtext.split_parts(text)
        lens = [xtext.weighted_len(p) for p in parts]
        if len(parts) == 1:
            note = f"{lens[0]} chars"
            if lens[0] > xtext.FOLD:
                note += " · Premium long post: the first 280 chars show before 'Show more'"
        else:
            note = f"Thread of {len(parts)} · " + " · ".join(
                f"{i}: {n}{' ⚠️' if n > xtext.FOLD else ''}" for i, n in enumerate(lens, 1))
        note += f" · v{v.version} by {v.created_by}"
        st.caption(note)

        if is_owner():
            _owner_actions(d, v, parts, text, text_key)

        n_src = len(d.inspiration or [])
        label = f"Inspiration & numbers · {n_src} source{'s' if n_src != 1 else ''}"
        if flags:
            label += f" · ⚠️ {len(flags)} check{'s' if len(flags) != 1 else ''}"
        with st.expander(label):
            for f in flags:
                st.warning(f, icon="🔎")
            if idea:
                st.markdown(f"🛠 **Build:** {esc_md(idea.title)} · status `{idea.status}`" +
                            (f" · [open build]({idea.shipped_url})" if idea.shipped_url else " · ⚠️ no link yet"))
            for src_ in d.inspiration or []:
                m = src_.get("metrics") or {}
                eng = f" · ♥ {m.get('like_count', 0):,} · 🔁 {m.get('retweet_count', 0):,}" if m else ""
                who = src_.get("author", "")
                who = f"@{who}" if src_.get("label") == "x_post" else who
                st.markdown(f'<div class="xcp-src"><b>{esc_html(who)}</b><span class="xcp-muted">{eng}</span><br>'
                            f'{esc_html((src_.get("text") or "")[:400])}</div>', unsafe_allow_html=True)
                if src_.get("url"):
                    st.markdown(f"[Open source ↗]({src_['url']})")
            if d.numbers:
                st.caption("Numbers used (checked against the market snapshot)")
                st.dataframe(d.numbers, hide_index=True, width="stretch")
            if d.chart_hint and d.chart_hint != "none":
                if st.button(f"📈 Render chart: {charts.CHARTS.get(d.chart_hint, d.chart_hint)}", key=f"ch_{d.id}"):
                    png, caption = charts.render(d.chart_hint, handle())
                    if png:
                        st.image(png, caption=caption)
                        st.download_button("⬇️ Download PNG to attach", png, file_name=f"{d.chart_hint}.png",
                                           mime="image/png", key=f"chdl_{d.id}")
                    else:
                        st.info(caption)

        with st.expander("Version history"):
            for h in data["history"].get((d.id, v.label), []):
                cols = st.columns([5, 1])
                cols[0].markdown(f"**v{h.version}** · {h.created_by} · {fmt_ny(h.created_at)}  \n"
                                 f"{esc_md(xtext.join_parts(h.parts)[:280])}")
                if not h.is_current and is_owner():
                    cols[1].button("Restore", key=f"rs_{h.id}", on_click=_restore,
                                   args=(d.id, h.label, h.parts, h.style, h.version))


# ------------------------------------------------------------------ page

_today = today_ny()
_mix = cache.get(("feed", "mix"), lambda: analyst.mix(7), ttl=120)

with st.container(key="isle_feed_top"):  # floats over the world as one island
    top = st.columns([1.15, 1.5, 1.35], gap="large")
    with top[0]:
        day = st.date_input("Day", value=today_ny(), format="MM/DD/YYYY")
        day_drafts = _day(day.isoformat())["drafts"]
        for key, spec in SLOTS.items():
            if not days_match(spec.get("days"), day):
                continue
            posted = any(x.slot == key and x.status == "posted" for x in day_drafts)
            extra = f" · 🛠 {showcase.title(showcase.panel_for(day))}" if showcase.is_showcase(key, day) else ""
            extra += " · optional" if spec.get("optional") else ""
            st.markdown(f"{'✅' if posted else '⬜'} **{spec['label']}** at {spec['post_at']}{extra}")

    with top[1]:
        m = _mix
        st.markdown(f"**Mix meter** · last 7 days · {m['total']} posts")
        for group, target in m["targets"].items():
            share = m["shares"].get(group, 0.0)
            gap = share - target
            icon = "■" if abs(gap) <= 7 or m["total"] < 5 else ("▲" if gap > 0 else "▼")
            st.progress(min(share / 100, 1.0), text=f"{icon} {TARGET_LABELS.get(group, group)}: {share:.0f}% "
                                                        f"(target {target}%)")

    with top[2]:
        now = now_ny()
        upcoming = []
        for offset in range(0, 8):
            dd = now.date() + timedelta(days=offset)
            for key, spec in SLOTS.items():
                t = at_ny(dd, spec["post_at"])
                if days_match(spec.get("days"), dd) and t > now:
                    upcoming.append((t, key))
            if upcoming:
                break
        if upcoming:
            t, key = min(upcoming)
            mins = int((t - now).total_seconds() // 60)
            sc = f" · 🛠 {showcase.title(showcase.panel_for(t.date()))}" if showcase.is_showcase(key, t.date()) else ""
            st.metric("Next slot", f"{slot_label(key)} {t.strftime('%I:%M %p').lstrip('0')}",
                      f"in {mins // 60}h {mins % 60}m{sc}", delta_color="off")
        n_pending = len(_day(_today.isoformat())["pending"])
        if n_pending:
            st.caption(f"⏳ {n_pending} AI request(s) queued or running")
        if is_owner():
            with st.expander("✍️ Draft from a link or idea"):
                src_in = st.text_input("X post URL, article link, or a raw idea", key="dfl_src")
                angle = st.text_input("Your angle (optional)", key="dfl_angle")
                if st.button("Create drafts", type="primary", key="dfl_go") and src_in.strip():
                    if src_in.strip().startswith("http"):
                        enqueue("draft_from_url", {"url": src_in.strip(), "angle": angle})
                    else:
                        enqueue("draft_from_text", {"text": src_in.strip(), "angle": angle})
            with st.expander("✨ Spark post ideas from my style library"):
                n_sp = st.slider("How many", 3, 10, 5, key="sp_n")
                focus = st.text_input("Focus (optional)", key="sp_focus", placeholder="e.g. STRC daily dividends")
                st.caption("Each idea uses a different format from your style library. Most are short, with at most one long.")
                if st.button("Spark ideas", type="primary", key="sp_go"):
                    enqueue("style_sparks", {"n": n_sp, "focus": focus})

def _recheck(panel: str) -> None:
    ok, why = gh.dispatch("showcase.yml", {"mode": "once", "panel": panel})
    st.toast("Re-check started in the cloud. The result lands here in 2–4 minutes." if ok
             else f"Couldn't start the re-check: {why}", icon="🛰️" if ok else "⚠️")


# today's showcase status
sc_panel = showcase.panel_for(day)
def _fetch_run(day_iso: str, panel: str):
    with db.session() as s:
        return showcase.run_for(s, day_iso, panel)


if sc_panel:
    sc_run = cache.get(("feed", "scrun", day.isoformat(), sc_panel), lambda: _fetch_run(day.isoformat(), sc_panel),
                       ttl=FEED_TTL)
    spec = showcase.panels().get(sc_panel, {})
    with st.container(border=True, key=card_key("hot" if sc_run and sc_run.status == "ready" else "card", "scstatus")):
        c = st.columns([5, 1.3])
        if sc_run is None:
            c[0].markdown(f"🛠 **Showcase: {spec.get('title')}** · watcher starts {spec.get('start')} ET · "
                          f"post {spec.get('post')}")
        else:
            why = "; ".join(b.get("detail", "") for b in (sc_run.blockers or []))[:300]
            c[0].markdown(f"🛠 **Showcase: {spec.get('title')}** · {RUN_ICONS.get(sc_run.status, '')} "
                          f"**{sc_run.status}** · checked {fmt_ago(sc_run.updated_at)} · {sc_run.renders} render(s)"
                          + (f"  \n<span class='xcp-muted'>{esc_html(why)}</span>" if why and sc_run.status in
                             ("waiting", "blocked", "missed") else ""), unsafe_allow_html=True)
        if is_owner() and day == today_ny():
            c[1].button("🔄 Re-check now", key="sc_recheck", on_click=_recheck, args=(sc_panel,), width="stretch",
                        help="Render + audit the panel now in the cloud (about 2–4 minutes)")

f = st.columns([2, 2.4, 2, 1.1])
slot_opts = [k for k in SLOT_ORDER]
sel_slots = f[0].multiselect("Slot", slot_opts, format_func=slot_label, placeholder="All slots")
sel_status = f[1].multiselect("Status", list(STATUS_ICONS), default=["new", "edited", "snoozed", "posted"],
                              format_func=lambda x: f"{STATUS_ICONS[x]} {x}")
sel_pillars = f[2].multiselect("Pillar", list(config.pillars()), placeholder="All pillars",
                               format_func=lambda p: config.pillars()[p].get("label", p))
bank = f[3].toggle("⭐ Bank", help="Show your evergreen bank (all days)")

feed_data = _day("bank" if bank else day.isoformat(), bank)
drafts = list(feed_data["drafts"])
if not bank and sel_status:
    drafts = [x for x in drafts if x.status in sel_status]
if sel_slots:
    drafts = [x for x in drafts if x.slot in sel_slots]
if sel_pillars:
    drafts = [x for x in drafts if x.pillar in sel_pillars]
drafts.sort(key=lambda x: (SLOT_ORDER.index(x.slot) if x.slot in SLOT_ORDER else 99, x.kind != "showcase", -x.score))

if not drafts:
    if settings.get("writer", {}).get("mode", "monitor") == "monitor":
        st.info("No posts here yet. In Monitor mode the Feed holds the posts you write: press ✍️ Write on any story "
                "or brief on the Monitor page and save it here. Showcase images land here too.", icon="✍️")
    else:
        first_slot = next(iter(SLOTS.values()))
        st.info(f"No drafts here yet. The agents run on schedule (first slot of the day at {first_slot['run_at']} ET). "
                f"You can also draft from a link above, or start a run from Control Room.", icon="🛰️")

current = None
for d in drafts:
    if d.slot != current:
        current = d.slot
        spec = SLOTS.get(d.slot, {})
        note = f"post at {spec['post_at']}" if spec.get("post_at") else ""
        if showcase.is_showcase(d.slot, day):
            note += f" · showcase: {showcase.title(showcase.panel_for(day))}"
        section(slot_label(d.slot), note)
    render_draft(d, feed_data)
