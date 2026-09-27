"""📡 Monitor: post ideas under the times you post, the live wire, your 7 accounts, and what you saved.

You write the posts. Ideas sit three to a row under each post time. Open one and it spans the page with the news,
the numbers and a source under every item; write next to it, or move it to the Writer tab and keep it in view.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

import streamlit as st
from sqlalchemy import select

from panel import cache
from panel.common import (page, PILLAR_GLYPHS, badge, card_key, esc_html, esc_md, hero, is_owner, last_monitor_run,
                          pillar_badge)
from xcp import config, db, gh, ideas, showcase, xtext
from xcp.agents import monitor
from xcp.timeutil import aware, fmt_ago, fmt_ny, now_ny, parse_iso, today_ny, utcnow

page("MONITOR")  # this page's tab title and pixel icon (bookmarks pick them up)

PILLARS = config.pillars()
LABEL = {k: v.get("label", k) for k, v in PILLARS.items()}
KIND_LABEL = {"news": "News", "official": "Official", "filing": "SEC filings", "x_post": "X"}
VIEWS = ["🗞 Idea feed", "✍️ Writer", "📡 Live wire", "🎙 Your 7", "⭐ Saved"]
AI_GROUP = ("ai_models", "ai_benchmarks", "physical_ai")
CATS = ["digital_credit", "stablecoins", "ai_payments", "legislation", "bitcoin", "macro", "ai"]
CAT_LABEL = {**{k: f"{PILLAR_GLYPHS.get(k, '•')} {LABEL.get(k, k)}" for k in CATS}, "ai": "◈ AI models & robots"}
PAGE = 9  # ideas per post time before "show more"
WRITER_KV = "monitor:writer"
owner = is_owner()


# ------------------------------------------------------------------ data (shared cache: clicks never wait on Neon)

def _stream(hours: int, **kw) -> list[dict]:
    return cache.get(("stream", hours, tuple(sorted(kw.items()))), lambda: monitor.stream(hours=hours, **kw), ttl=60)


def _briefs() -> list[dict]:
    return cache.get("briefs", ideas.load_briefs, ttl=60)


def _saved_briefs() -> list[dict]:
    return cache.get("briefs_saved", lambda: [b for b in ideas.load_briefs(since_hours=24 * 30)
                                              if b["status"] == "saved"], ttl=120)


def _bust() -> None:
    """After Check now / Refresh: the next read comes straight from the database."""
    cache.bust("stream", "briefs", "briefs_saved", "monitor_last_run")


def _mark_story(key: str, status: str) -> None:
    def f(stories: list[dict]) -> None:
        for c in stories:
            if c["key"] == key:
                c["status"] = status
    cache.update("stream", f)


def _mark(idea: dict, status: str, brief_status: str | None = None) -> None:
    """Reflect your own Save / Pass / write in every cached copy right away (no reload)."""
    if idea["kind"] == "brief":
        bs = brief_status or {"hidden": "dismissed", "": "new"}.get(status, status)

        def f(briefs: list[dict]) -> None:
            for b in briefs:
                if b["key"] == idea["key"]:
                    b["status"] = bs
        cache.update("briefs", f)
        cache.bust("briefs_saved")
    elif idea.get("story_key"):
        _mark_story(idea["story_key"], status)


def _writer() -> dict:
    if "_writer" not in st.session_state:
        saved = db.kv_get(WRITER_KV) if owner else None
        st.session_state["_writer"] = saved if isinstance(saved, dict) else {"pins": [], "texts": {}}
    return st.session_state["_writer"]


def _persist() -> None:
    if owner:
        db.kv_set(WRITER_KV, _writer())


def _safe(key: str) -> str:
    return card_key("x", key)[2:]


# ------------------------------------------------------------------ actions

def _goto(view: str) -> None:
    st.session_state["_mon_goto"] = view


def _toggle(key: str) -> None:
    st.session_state["mon_open"] = None if st.session_state.get("mon_open") == key else key


def _more(occ_key: str) -> None:
    st.session_state[f"more_{occ_key}"] = st.session_state.get(f"more_{occ_key}", PAGE) + PAGE


def _set_status(idea: dict, status: str) -> None:
    """status: saved | hidden (pass) | "" (back to new)."""
    if idea["kind"] == "brief":
        with db.session() as s:
            s.get(db.Brief, idea["brief_id"]).status = {"hidden": "dismissed", "": "new"}.get(status, status)
            s.commit()
    else:
        monitor.set_status(idea["story_key"], status)
    if status == "hidden" and st.session_state.get("mon_open") == idea["key"]:
        st.session_state["mon_open"] = None
    _mark(idea, status)
    st.toast({"saved": "Saved. Find it under ⭐ Saved.", "hidden": "Passed. It won't come back.",
              "": "Removed from Saved."}[status], icon="🗂")


def _pin(idea: dict) -> None:
    w = _writer()
    w["pins"] = [p for p in w["pins"] if p["key"] != idea["key"]] + [idea]
    st.session_state["wr_sel"] = idea["key"]
    _persist()
    _goto(VIEWS[1])


def _unpin(key: str) -> None:
    w = _writer()
    w["pins"] = [p for p in w["pins"] if p["key"] != key]
    _persist()


def _keep_text(wk: str, key: str) -> None:
    _writer()["texts"][key] = st.session_state.get(wk, "")
    _persist()


def _save_to_feed(idea: dict, wk: str) -> None:
    text = st.session_state.get(wk, "")
    if not text.strip():
        return
    with db.session() as s:
        d = db.Draft(slot="on_demand", slot_date=today_ny().isoformat(), kind="regular", status="edited",
                     pillar=idea.get("pillar") or "bitcoin", lane=config.pillar_lane(idea.get("pillar")),
                     tone="timely", title=idea["title"][:200], numbers=idea.get("numbers") or [],
                     inspiration=[{"label": x.get("kind", "news"), "url": x.get("url", ""),
                                   "author": x.get("publisher", ""), "text": x.get("title", "")}
                                  for x in idea.get("items", [])[:6]])
        s.add(d)
        s.flush()
        db.add_variant_version(s, d.id, "A", xtext.split_parts(text), "mine", "me")
        if idea.get("brief_id"):
            b = s.get(db.Brief, idea["brief_id"])
            b.status, b.draft_id = "used", d.id
        s.commit()
    if idea.get("story_key"):
        monitor.set_status(idea["story_key"], "used")
    _keep_text(wk, idea["key"])
    _mark(idea, "used", "used")
    cache.bust("feed")
    st.toast("Saved to the Feed. Post it from there or straight from X.", icon="✍️")


# ------------------------------------------------------------------ pieces

def _label_md(text: str) -> str:
    """Button labels render markdown: escape it so headlines show as typed."""
    out = str(text)
    for ch in "\\*_[]`~$#<>":
        out = out.replace(ch, "\\" + ch)
    return out


def _url(u) -> str:
    return u if isinstance(u, str) and u.startswith(("http://", "https://")) else ""


def _domain(u: str) -> str:
    try:
        return urlparse(u).netloc.removeprefix("www.")
    except ValueError:
        return ""


GAP_HOURS = 3  # show "latest …" too when newer coverage came this much later than the first report


def _surfaced(idea: dict):
    """When the underlying event first surfaced (its earliest coverage), not when the latest repost landed."""
    s = parse_iso(idea.get("surfaced"))
    if s is None:  # pinned before first-surfaced times existed
        dated = [parse_iso(x["at"]) for x in idea.get("items", []) if x.get("at")]
        s = min(dated) if dated else parse_iso(idea.get("newest"))
    return s


def _is_new(idea: dict) -> bool:
    """NEW! only when the event itself is under three hours old (a fresh repost of old news doesn't count)."""
    first, surfaced = parse_iso(idea.get("first_seen")), _surfaced(idea)
    return bool(first and surfaced and utcnow() - first < timedelta(minutes=30)
                and utcnow() - surfaced < timedelta(hours=GAP_HOURS))


def _cat(pillar: str) -> str:
    return "ai" if pillar in AI_GROUP else pillar


def _chips(idea: dict, full: bool = False) -> str:
    chips = [pillar_badge(idea["pillar"])] if full else []
    if idea["kind"] == "brief":
        chips.append(badge("🧠 desk pick", "ink"))
    if idea.get("priority"):
        chips.append(badge("⚡ " + idea["priority"][:36], "hot" if idea["hot"] else "paper"))
    if _is_new(idea):
        chips.append(badge("NEW!", "new"))
    if idea.get("watchlist"):
        chips.append(badge("🎙 your 7", "ink"))
    if idea.get("status") in ("saved", "used"):
        chips.append(badge("⭐ saved" if idea["status"] == "saved" else "✅ written", "paper"))
    if full and idea.get("publishers", 0) > 1:
        chips.append(badge(f"{idea['publishers']} outlets", "tan"))
    return " ".join(chips)


def _meta(idea: dict) -> str:
    items = idea.get("items", [])
    lead = items[0] if items else {}
    surfaced, newest = _surfaced(idea), parse_iso(idea.get("newest"))
    n = len(items)
    bits = [f"{n} source{'s' if n != 1 else ''}"] if n else []
    if lead.get("publisher"):
        bits.append(lead["publisher"])
    if surfaced:
        bits.append(f"surfaced {fmt_ago(surfaced)}")
        latest = max([parse_iso(x["at"]) for x in items if x.get("at")] or [newest or surfaced])
        if latest - surfaced > timedelta(hours=GAP_HOURS):
            bits.append(f"latest {fmt_ago(latest)}")
    return " · ".join(bits)


def _surfaced_html(idea: dict) -> str:
    """The opened idea's time line: when and where the event first surfaced, and the latest coverage."""
    at = _surfaced(idea)
    if at is None:
        return ""
    via = idea.get("surfaced_via") or {}
    url = _url(via.get("url"))
    who = esc_html(via.get("publisher") or _domain(url) or "")
    src = (f' · first report: <a href="{esc_html(url)}" target="_blank" rel="noopener">{who} ↗</a>' if url and who
           else (f" · first report: {who}" if who else ""))
    if via.get("title") and url not in {x.get("url") for x in idea.get("items", [])}:
        src += f' <span class="t">"{esc_html(via["title"][:120])}"</span>'  # found in earlier, separate coverage
    items = [parse_iso(x["at"]) for x in idea.get("items", []) if x.get("at")]
    latest = max(items) if items else parse_iso(idea.get("newest"))
    tail = (f" · latest coverage {fmt_ago(latest)}" if latest and latest - at > timedelta(hours=GAP_HOURS) else "")
    return (f'<div class="xcp-surf"><b>FIRST SURFACED</b> {fmt_ny(at, "%a %b %d, %I:%M %p").replace(", 0", ", ")} ET · {fmt_ago(at)}'
            f'{src}{tail}</div>')


def _news_html(items: list[dict], first_url: str | None = None) -> str:
    out = []
    for x in items:
        url = _url(x.get("url"))
        at = parse_iso(x.get("at")) if x.get("at") else None
        head = f"{x.get('publisher', '')} on X" if x.get("kind") == "x_post" else x.get("title", "")
        body = x.get("snippet", "")
        who = esc_html(x.get("publisher") or _domain(url) or "source")
        src = [f'<a href="{esc_html(url)}" target="_blank" rel="noopener">{who} ↗</a>' if url else who]
        if url and _domain(url):
            src.append(esc_html(_domain(url)))
        if at:
            src.append(f"{'posted' if x.get('kind') == 'x_post' else 'published'} {fmt_ago(at)}")
        if first_url and url == first_url:
            src.append("<b class='first'>FIRST REPORT</b>")
        if x.get("official"):
            src.append("official")
        mt = x.get("metrics") or {}
        if x.get("kind") == "x_post" and mt:
            src.append(f"♥ {mt.get('like_count', 0):,} · 🔁 {mt.get('retweet_count', 0):,}")
        title = (f'<a href="{esc_html(url)}" target="_blank" rel="noopener">{esc_html(head)}</a>' if url
                 else esc_html(head))
        out.append(f'<div class="xcp-news"><div class="h">{title}</div>'
                   + (f'<div class="s">{esc_html(body)}</div>' if body else "")
                   + f'<div class="src"><b>SOURCE</b> {" · ".join(src)}</div></div>')
    return "".join(out)


def _lbl(text: str) -> None:
    st.markdown(f'<div class="xcp-lbl">{esc_html(text)}</div>', unsafe_allow_html=True)


def idea_body(idea: dict) -> None:
    st.markdown(f"<div class='xcp-chips'>{_chips(idea, full=True)}</div>", unsafe_allow_html=True)
    st.markdown(f'<div class="xcp-idea-h">{esc_html(idea["title"])}</div>', unsafe_allow_html=True)
    if (line := _surfaced_html(idea)):
        st.markdown(line, unsafe_allow_html=True)
    if idea.get("what"):
        _lbl("What happened")
        st.markdown(esc_md(idea["what"]))
    if idea.get("why"):
        _lbl("Why it matters")
        st.markdown(esc_md(idea["why"]))
    if idea.get("numbers"):
        _lbl("The numbers")
        st.dataframe([{"": x.get("label", ""), "value": x.get("value", ""), "source": x.get("source", "")}
                      for x in idea["numbers"]], hide_index=True, width="stretch")
    if idea.get("flags"):
        st.caption("⚠️ Check before using: " + "; ".join(idea["flags"]))
    if idea.get("angles"):
        _lbl("Ways in")
        st.markdown("\n".join(f"- {esc_md(a)}" for a in idea["angles"]))
    items = idea.get("items", [])
    _lbl(f"The news · {len(items)} source{'s' if len(items) != 1 else ''}")
    first_url = (idea.get("surfaced_via") or {}).get("url")
    st.markdown(_news_html(items, first_url) or "<div class='xcp-muted'>No sources on file.</div>",
                unsafe_allow_html=True)
    more = ideas.related(idea, POOL)
    if more:
        _lbl(f"More on this · {len(more)} related stor{'ies' if len(more) != 1 else 'y'}")
        st.markdown(_news_html([x["items"][0] for x in more if x.get("items")]), unsafe_allow_html=True)


def composer(idea: dict, where: str, big: bool = False) -> None:
    safe = _safe(idea["key"])
    wk = f"cmp_{where}_{safe}"
    with st.container(key=f"composer_{where}_{safe}"):
        st.markdown('<div class="xcp-comp-h">✍ Your post</div>', unsafe_allow_html=True)
        if not owner:
            st.caption("View only. Unlock with 🔑 Owner to write.")
            return
        if wk not in st.session_state:
            st.session_state[wk] = _writer()["texts"].get(idea["key"], "")
        text = st.text_area("Your post", key=wk, height=400 if big else 230, label_visibility="collapsed",
                            on_change=_keep_text, args=(wk, idea["key"]),
                            placeholder="Your take, in your words. A line with only --- starts the next post "
                                        "in a thread. Ctrl+Enter locks the text in.")
        parts = xtext.split_parts(text) if text.strip() else []
        n = xtext.weighted_len(parts[0]) if parts else 0
        st.caption(f"{n}/280" + (f" · thread of {len(parts)}" if len(parts) > 1 else "")
                   + (" · long post: land the hook in the first 280" if n > 280 else ""))
        first = parts[0] if parts else ""
        quote = idea.get("quote_url") or (idea["items"][0]["url"] if idea.get("items")
                                          and idea["items"][0].get("kind") == "x_post" else None)
        link = xtext.intent_quote(quote, first) if quote else xtext.intent_post(first)
        c = st.columns(2)
        c[0].link_button("🚀 Open in X" + (" (quote)" if quote else ""), link, type="primary", width="stretch",
                         disabled=not first)
        c[1].button("💾 Save to Feed", key=f"sv_{wk}", on_click=_save_to_feed, args=(idea, wk), width="stretch",
                    disabled=not first)
        c = st.columns(2)
        if where == "writer":
            c[0].button("📌 Unpin", key=f"up_{wk}", on_click=_unpin, args=(idea["key"],), width="stretch")
        else:
            c[0].button("↗ Writer tab", key=f"pin_{wk}", on_click=_pin, args=(idea,), width="stretch",
                        help="Pin this idea next to a bigger editor (it stays there until you unpin it)")
            c[1].button("▴ Close", key=f"cl_{wk}", on_click=_toggle, args=(idea["key"],), width="stretch")


def detail(idea: dict, where: str, occ: dict | None = None) -> None:
    """The opened idea: spans the page, the news on the left, your post on the right."""
    safe = _safe(idea["key"])
    with st.container(key=f"detail_{where}_{safe}"):
        slot = f" · for {occ['label']} {occ['post_at'].strftime('%a %I:%M %p').replace(' 0', ' ')}" if occ else ""
        with st.container(horizontal=True, vertical_alignment="center", gap=None, key=f"dtb_{where}_{safe}"):
            st.markdown(f'<span class="xcp-tb-l">IDEA · <span class="jp">案</span> — '
                        f'{esc_html(LABEL.get(idea["pillar"], idea["pillar"]))}{esc_html(slot)}</span>', unsafe_allow_html=True)
            st.button("", icon=":material/minimize:", key=f"wmin_{where}_{safe}", on_click=_toggle,
                      args=(idea["key"],), help="Minimize")
            st.button("", icon=":material/crop_square:", key=f"wmax_{where}_{safe}", on_click=_pin, args=(idea,),
                      help="Maximize: open it in the Writer tab")
            st.button("", icon=":material/close:", key=f"wx_{where}_{safe}", on_click=_toggle, args=(idea["key"],),
                      help="Close")
        with st.container(key=f"dbody_{where}_{safe}"):
            left, right = st.columns([1.55, 1], gap="large")
            with left:
                idea_body(idea)
            with right:
                composer(idea, where)


def tile(idea: dict, where: str) -> None:
    """Collapsed idea: what the post is on. Click the headline to open it full width."""
    k, safe = idea["key"], _safe(idea["key"])
    is_open = st.session_state.get("mon_open") == k
    prefix = "open" if is_open else ("hot" if idea["hot"] else "card")
    with st.container(border=True, key=f"{prefix}_{where}_{safe}"):
        topic = LABEL.get(idea["pillar"], idea["pillar"])
        st.markdown(f'<div class="xcp-poston"><span></span>Post on · {PILLAR_GLYPHS.get(idea["pillar"], "")} '
                    f'{esc_html(topic)}</div>', unsafe_allow_html=True)
        chips = _chips(idea)
        if chips:
            st.markdown(f"<div class='xcp-chips'>{chips}</div>", unsafe_allow_html=True)
        title = idea["title"] if len(idea["title"]) <= 150 else idea["title"][:147].rstrip() + "…"
        st.button(_label_md(title), key=f"ih_{where}_{safe}", on_click=_toggle,
                  args=(k,), width="stretch", help="Close" if is_open else "Open the news, numbers and sources")
        st.markdown(f"<div class='xcp-muted'>{esc_html(_meta(idea))}</div>", unsafe_allow_html=True)
        if owner:
            with st.container(horizontal=True, gap="small", key=f"acts_{where}_{safe}"):
                saved = idea.get("status") == "saved"
                st.button("★ Saved" if saved else "☆ Save", key=f"is_{where}_{safe}", on_click=_set_status,
                          args=(idea, "" if saved else "saved"))
                st.button("✕ Pass", key=f"ip_{where}_{safe}", on_click=_set_status, args=(idea, "hidden"),
                          help="Not posting about this. It won't come back.")


def grid(items: list[dict], where: str, occ: dict | None = None) -> None:
    """Three to a row; an opened idea unfolds full width right under its row."""
    open_k = st.session_state.get("mon_open")
    tag = _safe(occ["key"] if occ else where)
    for r in range(0, len(items), 3):
        row = items[r:r + 3]
        with st.container(key=f"irow_{tag}_{r}"):
            cols = st.columns(3)
            for col, idea in zip(cols, row):
                with col:
                    tile(idea, where)
        for idea in row:
            if idea["key"] == open_k:
                detail(idea, where, occ)


def _hm(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m" if h < 24 else f"{h // 24}d {h % 24}h"


def slot_header(o: dict, n: int, first_up: bool, now) -> None:
    t = o["post_at"]
    hh, ampm = t.strftime("%I:%M").lstrip("0"), t.strftime("%p")
    day = ("Today" if t.date() == now.date() else "Tomorrow" if t.date() == (now + timedelta(days=1)).date()
           else "Yesterday" if t.date() == (now - timedelta(days=1)).date() else t.strftime("%A"))
    right = []
    if o["passed"]:
        right.append(badge("passed", "paper"))
    elif first_up:
        right.append(badge(f"up next · in {_hm(t - now)}", "hot"))
    else:
        right.append(badge(f"in {_hm(t - now)}", "tan"))
    if o.get("panel"):
        status = cache.get(("showcase_status", o["key"]), lambda: ideas.showcase_status(o), ttl=60) or "waiting"
        right.append(badge(f"🖼 {showcase.title(o['panel'])} · {status}",
                           "olive" if status in ("ready", "posted") else "ink"))
    lane = "BTC lane · digital credit, stablecoins, legislation, bitcoin, macro" if o["lane"] == "btc" else "AI lane"
    run = f" · desk picks land at {o['run_at']}" if o.get("run_at") and not o["passed"] else ""
    cls = "xcp-slot" + (" next" if first_up else "") + (" past" if o["passed"] else "")
    st.markdown(f'<div class="{cls}"><div class="t">{hh}<small>{ampm}</small></div>'
                f'<div class="m"><div class="l">{esc_html(o["label"])} · {day}</div>'
                f'<div class="s">{esc_html(lane)} · {n} idea{"s" if n != 1 else ""}{esc_html(run)}</div></div>'
                f'<div class="r">{" ".join(right)}</div></div>', unsafe_allow_html=True)


# ------------------------------------------------------------------ live wire cards (raw stories)

def story_card(c: dict, where: str = "live") -> None:
    lead = c["lead"]
    published = lead.created_at or lead.fetched_at
    idea = ideas.stamp(ideas.from_story(c), INDEX)
    with st.container(border=True, key=card_key("hot" if c["priority"] else "card", f"{where}_{c['key']}")):
        body, side = (st.container(), None) if not owner else st.columns([7, 2.1], vertical_alignment="center")
        with body:
            who = f"@{lead.author}" if lead.kind == "x_post" else (lead.author or lead.source)
            more = f" · +{c['publishers'] - 1} outlets" if c["publishers"] > 1 else ""
            when = f"{'posted' if lead.kind == 'x_post' else 'published'} {fmt_ago(published)}"
            surfaced = _surfaced(idea)
            if surfaced and aware(published) - surfaced > timedelta(hours=GAP_HOURS):
                when += f" · story first surfaced {fmt_ago(surfaced)}"
            st.markdown(f"{pillar_badge(c['pillar'])} {_chips(idea)} <span class='xcp-muted'>{esc_html(who)} · "
                        f"{when}{more}</span>", unsafe_allow_html=True)
            if lead.kind == "x_post":
                mt = lead.metrics or {}
                st.markdown(esc_md(" ".join((lead.text or "").split())[:500]))
                st.caption(f"♥ {mt.get('like_count', 0):,} · 🔁 {mt.get('retweet_count', 0):,} · "
                           f"💬 {mt.get('reply_count', 0):,} · [open on X ↗]({lead.url})")
            else:
                st.markdown(f"**[{esc_md(c['title'][:160])}]({lead.url})**")
                snippet = idea["items"][0]["snippet"][:240] if idea["items"] else ""
                if snippet:
                    st.caption(snippet)
            if c["publishers"] > 1:
                with st.expander(f"{c['publishers']} outlets on this"):
                    st.markdown(_news_html(idea["items"], (idea.get("surfaced_via") or {}).get("url")),
                                unsafe_allow_html=True)
        if owner:
            with side:
                with st.container(horizontal=True, horizontal_alignment="right", gap="small"):
                    if st.button("✍️ Write", key=f"w_{where}_{c['key']}", type="primary",
                                 help="Open it in the Writer tab"):
                        _pin(idea)
                        st.rerun()
                    if st.button("⭐", key=f"sv_{where}_{c['key']}", help="Save for later"):
                        new = "" if c["status"] == "saved" else "saved"
                        monitor.set_status(c["key"], new)
                        _mark_story(c["key"], new)
                        st.rerun()
                    if st.button("🙈", key=f"hd_{where}_{c['key']}", help="Hide this story"):
                        monitor.set_status(c["key"], "hidden")
                        _mark_story(c["key"], "hidden")
                        st.rerun()


# ------------------------------------------------------------------ page

def _check_now() -> None:
    with st.spinner("Checking every news source…"):
        news = monitor.ingest_news()
        monitor.cluster_recent()
        monitor.evaluate(news["new_ids"], ping=False)
    _bust()
    ok = gh.can_dispatch() and gh.dispatch("monitor.yml")[0]  # the cloud run adds X and SEC filings
    st.toast(f"{len(news['new_ids'])} new stories from the news feeds"
             + (" · X and SEC filings arrive in about a minute" if ok else ""), icon="📡")


if "_mon_goto" in st.session_state:
    st.session_state["mon_view"] = st.session_state.pop("_mon_goto")
st.session_state.setdefault("mon_view", VIEWS[0])

now = now_ny()
last = last_monitor_run()
occs = ideas.occurrences(now)
stories72 = _stream(72)
INDEX = cache.get("event_index", ideas.load_event_index, ttl=600, wait=False)  # 14 days of headlines
groups = ideas.assign(stories72, _briefs(), occs, now, INDEX)
POOL = [x for L in groups.values() for x in L]  # for "More on this" under an opened idea
upcoming = [o for o in occs if not o["passed"]]
nxt = upcoming[0] if upcoming else None
cut12, cut24 = utcnow() - timedelta(hours=12), utcnow() - timedelta(hours=24)
_pri = sum(1 for c in stories72 if c["priority"] and c["status"] != "hidden" and c["newest"] >= cut12)
_watch24 = sum(1 for c in stories72 if c["lead"].kind == "x_post" and (c["lead"].meta or {}).get("watchlist")
               and c["newest"] >= cut24)
_n_sources = len(monitor.feeds()) + len(monitor.watchlist_handles()) + 2
hero("MONITOR.EXE", "Ideas for your <em>next post</em>.",
     "Ideas sit under the times you post. Open one to see the news, the numbers and every source full width, "
     "then write next to it or move it to the Writer tab. You write the posts.",
     stats=[(nxt["post_at"].strftime("%I:%M %p").lstrip("0") if nxt else "—",
             f"next · {nxt['label'].split(' ', 1)[-1]}" if nxt else "next post", True),
            (len(groups.get(nxt["key"], [])) if nxt else 0, "ideas for it"),
            (_pri, "⚡ priority · 12h", _pri > 0), (_watch24, "🎙 your 7 · 24h")],
     kicker=(f"live wire · checked {fmt_ago(parse_iso(last['at']))} · {_n_sources} sources" if last.get("at")
             else f"live wire · {_n_sources} sources"), icon="📡")
if owner:
    tb = st.columns([5, 1.3])
    tb[0].caption(f"Last check: {last.get('news_new', 0)} new stories · {last.get('x_new', 0)} new posts from your "
                  f"accounts · {last.get('priority', 0)} flagged · Discord only pings when a Digital Credit Report "
                  f"panel goes live" if last.get("at") else "The monitor hasn't run yet.")
    tb[1].button("🔄 Check now", on_click=_check_now, width="stretch", type="primary",
                 help="Pulls the news feeds right away; X and SEC filings follow from the cloud")

pins = _writer()["pins"]
view = st.segmented_control("View", VIEWS, key="mon_view", required=True, label_visibility="collapsed",
                            format_func=lambda v: v + (f" ({len(pins)})" if v == VIEWS[1] and pins else ""),
                            width="stretch")

# ------------------------------------------------------------------ 🗞 idea feed
if view == VIEWS[0]:
    f = st.columns([4.6, 1.7, 1])
    cats = f[0].pills("Categories", CATS, format_func=CAT_LABEL.get, selection_mode="multi", key="if_cats")
    pri_only = f[1].toggle("⚡ Priority only", key="if_pri")
    f[2].button("↻ Refresh", on_click=_bust, width="stretch", key="if_ref")

    def keep(x: dict) -> bool:
        return (not cats or _cat(x["pillar"]) in cats) and (not pri_only or x["hot"] or bool(x.get("priority")))

    shown = 0
    for o in occs:
        items = [x for x in groups.get(o["key"], []) if keep(x)]
        if o["passed"] and not items:
            continue
        slot_header(o, len(items), o is nxt, now)
        if not items:
            st.caption("Nothing here yet. It fills as news breaks; the desk adds researched picks at "
                       f"{o.get('run_at') or 'the slot run'}.")
            continue
        n = st.session_state.get(f"more_{o['key']}", PAGE)
        grid(items[:n], "feed", o)
        shown += 1
        if len(items) > n:
            st.button(f"▾ Show {min(PAGE, len(items) - n)} more · {len(items) - n} left for this slot",
                      key=f"more_btn_{o['key']}", on_click=_more, args=(o["key"],), width="stretch")
    if not shown and (cats or pri_only):
        st.info("No ideas match these filters right now.", icon="🗞")

# ------------------------------------------------------------------ ✍️ writer
elif view == VIEWS[1]:
    if not pins:
        st.info("Open any idea and press ↗ Writer tab. It stays pinned here next to a bigger editor until you "
                "unpin it, and your text is kept.", icon="✍️")
    else:
        keys = [p["key"] for p in reversed(pins)]
        if st.session_state.get("wr_sel") not in keys:
            st.session_state["wr_sel"] = keys[0]
        by_key = {p["key"]: p for p in pins}
        sel = st.pills("Pinned ideas", keys, key="wr_sel", selection_mode="single",
                       format_func=lambda k: by_key[k]["title"][:46] + ("…" if len(by_key[k]["title"]) > 46 else ""))
        idea = by_key[sel or keys[0]]
        fresh = {x["key"]: x for L in groups.values() for x in L}  # newer outlets / numbers since it was pinned
        idea = fresh.get(idea["key"]) or ideas.stamp(dict(idea), INDEX)
        left, right = st.columns([1.4, 1], gap="large")
        with left:
            with st.container(height=700, key="wr_scroll"):
                idea_body(idea)
        with right:
            composer(idea, "writer", big=True)

# ------------------------------------------------------------------ 📡 live wire
elif view == VIEWS[2]:
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
        stories = _stream(int(hours), include_offtopic=offlane)
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

# ------------------------------------------------------------------ 🎙 your 7
elif view == VIEWS[3]:
    handles = sorted({a["handle"].lstrip("@") for a in config.get("watchlist").get("accounts", []) if a.get("handle")})
    who = st.pills("Accounts", handles, selection_mode="multi", key="mon_who")

    @st.fragment(run_every="60s")
    def watch() -> None:
        posts = _stream(48, kinds=("x_post",), watchlist_only=True, include_offtopic=True)
        if who:
            wanted = {h.lower() for h in who}
            posts = [c for c in posts if (c["lead"].author or "").lower() in wanted]
        posts = sorted((c for c in posts if c["status"] != "hidden"), key=lambda c: c["newest"], reverse=True)
        st.caption(f"{len(posts)} posts in the last 48 hours · checked every 15 minutes")
        for c in posts[:60]:
            story_card(c, where="watch")
        if not posts:
            st.info("No posts from your accounts in the last 48 hours yet.", icon="🎙")

    watch()
    with db.session() as s:
        replies_b = list(s.scalars(select(db.Brief).where(db.Brief.kind == "reply_target",
                                                          db.Brief.created_at >= utcnow() - timedelta(hours=36),
                                                          db.Brief.status != "dismissed")
                                   .order_by(db.Brief.created_at.desc()).limit(6)).all())
    if replies_b:
        st.markdown('<div class="xcp-sec"><span class="bar"></span><h3>Worth replying to</h3>'
                    '<span class="note">picked by the desk</span></div>', unsafe_allow_html=True)
        for b in replies_b:
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

# ------------------------------------------------------------------ ⭐ saved
else:
    saved = [ideas.stamp(ideas.from_story(c), INDEX) for c in _stream(24 * 7, include_offtopic=True)
             if c["status"] == "saved"]
    saved_b = [ideas.stamp(b, INDEX) for b in _saved_briefs() if b["status"] == "saved"]
    items = saved_b + sorted(saved, key=lambda x: x["newest"], reverse=True)
    if not items:
        st.info("Nothing saved yet. Press ☆ Save on any idea.", icon="⭐")
    grid(items, "saved")
