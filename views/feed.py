"""📰 Feed: X-style draft cards with editing, one-click rewrites and posting."""
from __future__ import annotations

from datetime import timedelta

import streamlit as st
from sqlalchemy import select

from panel.common import STATUS_ICONS, badge, enqueue, esc_html, esc_md, handle, is_owner, pillar_badge
from xcp import charts, config, db, showcase, xtext
from xcp.agents import analyst
from xcp.timeutil import at_ny, days_match, fmt_ago, fmt_ny, now_ny, today_ny, utcnow

settings = config.settings()
SLOTS = settings["slots"]
SLOT_ORDER = list(SLOTS) + ["on_demand"]
TARGET_LABELS = {"digital_credit": "Digital credit", "bitcoin": "Bitcoin", "macro": "Macro", "ai": "AI"}
REWRITES = [("shorter", "Shorter"), ("punchier", "Punchier"), ("more_data", "+ Data"), ("funnier", "Funnier"),
            ("hook", "Hook"), ("thread", "Thread"), ("single", "Single")]
DISMISS_REASONS = ["Off-topic", "Stale / too late", "Not my voice", "Weak angle", "Factually shaky", "Other"]


def slot_label(key: str) -> str:
    return SLOTS.get(key, {}).get("label", "✍️ On demand" if key == "on_demand" else key)


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
        s.commit()
        pillar, tone, kind, style = d.pillar, d.tone, d.kind, v.style if v else None
    analyst.log_post(draft_id, url, text.replace(xtext.THREAD_SEP, "\n\n"), pillar, tone, style, kind)
    st.toast("Logged as posted. It counts toward your mix now.", icon="✅")


def _set_status(draft_id: int, status: str, reason_key: str | None = None) -> None:
    with db.session() as s:
        d = s.get(db.Draft, draft_id)
        d.status = status
        if reason_key:
            d.dismiss_reason = st.session_state.get(reason_key)
        s.commit()


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
                    st.toast(f"Snoozed to {slot_label(key)} {day:%a}", icon="⏰")
                    return


def _restore(draft_id: int, label: str, parts: list[str], style: str, version: int) -> None:
    with db.session() as s:
        db.add_variant_version(s, draft_id, label, parts, style, f"restore:v{version}")
        s.commit()


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


def render_draft(d: db.Draft) -> None:
    with db.session() as s:
        variants = db.current_variants(s, d.id)
        pending = db.pending_requests(s, d.id)
        idea = s.get(db.BuildIdea, d.build_idea_id) if d.build_idea_id else None
    if not variants:
        return
    by_label = {v.label: v for v in variants}
    labels = list(by_label)
    flags = d.editor_flags or []

    with st.container(border=True):
        chips = []
        if d.kind == "showcase":
            chips.append(badge("🛠 SHOWCASE", "#E0245E"))
        chips += [pillar_badge(d.pillar), badge(d.tone, "#8899A6")]
        chips.append(f'<span class="xcp-muted">{STATUS_ICONS.get(d.status, "")} {d.status} · score {d.score:.1f} · '
                     f'{fmt_ago(d.created_at)}</span>')
        st.markdown(" ".join(chips), unsafe_allow_html=True)
        if d.title:
            st.markdown(f"**{esc_md(d.title)}**")
        if pending:
            st.info(f"⏳ {len(pending)} AI request(s) running for this draft. Refresh in a minute.", icon="🛰️")

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
            with db.session() as s:
                hist = s.scalars(select(db.Variant).where(db.Variant.draft_id == d.id, db.Variant.label == v.label)
                                 .order_by(db.Variant.version.desc())).all()
            for h in hist:
                cols = st.columns([5, 1])
                cols[0].markdown(f"**v{h.version}** · {h.created_by} · {fmt_ny(h.created_at)}  \n"
                                 f"{esc_md(xtext.join_parts(h.parts)[:280])}")
                if not h.is_current and is_owner():
                    cols[1].button("Restore", key=f"rs_{h.id}", on_click=_restore,
                                   args=(d.id, h.label, h.parts, h.style, h.version))


# ------------------------------------------------------------------ page

top = st.columns([1.15, 1.5, 1.35], gap="large")
with top[0]:
    day = st.date_input("Day", value=today_ny(), format="MM/DD/YYYY")
    with db.session() as s:
        day_drafts = s.scalars(select(db.Draft).where(db.Draft.slot_date == day.isoformat())).all()
    for key, spec in SLOTS.items():
        if not days_match(spec.get("days"), day):
            continue
        posted = any(x.slot == key and x.status == "posted" for x in day_drafts)
        extra = " · 🛠 showcase" if showcase.is_showcase(key, day) else ""
        extra += " · optional" if spec.get("optional") else ""
        st.markdown(f"{'✅' if posted else '⬜'} **{spec['label']}** at {spec['post_at']}{extra}")

with top[1]:
    m = analyst.mix(7)
    st.markdown(f"**Mix meter** · last 7 days · {m['total']} posts")
    for group, target in m["targets"].items():
        share = m["shares"].get(group, 0.0)
        gap = share - target
        icon = "🟢" if abs(gap) <= 7 or m["total"] < 5 else ("🔺" if gap > 0 else "🔻")
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
        sc = " · 🛠 showcase" if showcase.is_showcase(key, t.date()) else ""
        st.metric("Next slot", f"{slot_label(key)} {t.strftime('%I:%M %p').lstrip('0')}",
                  f"in {mins // 60}h {mins % 60}m{sc}", delta_color="off")
    with db.session() as s:
        n_pending = len(db.pending_requests(s))
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

f = st.columns([2, 2.4, 2, 1.1])
slot_opts = [k for k in SLOT_ORDER]
sel_slots = f[0].multiselect("Slot", slot_opts, format_func=slot_label, placeholder="All slots")
sel_status = f[1].multiselect("Status", list(STATUS_ICONS), default=["new", "edited", "snoozed", "posted"],
                              format_func=lambda x: f"{STATUS_ICONS[x]} {x}")
sel_pillars = f[2].multiselect("Pillar", list(config.pillars()), placeholder="All pillars",
                               format_func=lambda p: config.pillars()[p].get("label", p))
bank = f[3].toggle("⭐ Bank", help="Show your evergreen bank (all days)")

with db.session() as s:
    q = select(db.Draft).where(db.Draft.kind.in_(["regular", "showcase"]))
    q = q.where(db.Draft.status == "banked") if bank else q.where(db.Draft.slot_date == day.isoformat())
    drafts = list(s.scalars(q).all())
if not bank and sel_status:
    drafts = [x for x in drafts if x.status in sel_status]
if sel_slots:
    drafts = [x for x in drafts if x.slot in sel_slots]
if sel_pillars:
    drafts = [x for x in drafts if x.pillar in sel_pillars]
drafts.sort(key=lambda x: (SLOT_ORDER.index(x.slot) if x.slot in SLOT_ORDER else 99, x.kind != "showcase", -x.score))

if not drafts:
    first_slot = next(iter(SLOTS.values()))
    st.info(f"No drafts here yet. The agents run on schedule (first slot of the day at {first_slot['run_at']} ET). "
            f"You can also draft from a link above, or start a run from Control Room.", icon="🛰️")

current = None
for d in drafts:
    if d.slot != current:
        current = d.slot
        spec = SLOTS.get(d.slot, {})
        head = f"### {slot_label(d.slot)}"
        if spec.get("post_at"):
            head += f" · post at {spec['post_at']}"
        if showcase.is_showcase(d.slot, day):
            head += " · 🛠 showcase slot"
        st.markdown(head)
    render_draft(d)
