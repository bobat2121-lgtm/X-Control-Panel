"""⚙️ Control Room: agents, settings, watchlist, voice, market inputs, calendar."""
from __future__ import annotations

import copy

import pandas as pd
import streamlit as st
from sqlalchemy import select

from panel.common import enqueue, is_owner
from xcp import config, db, notify
from xcp.agents.collect import x_reads_today
from xcp.config import env
from xcp.timeutil import fmt_ny, today_ny

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CODEX_MODELS = {  # from OpenAI's Codex model list (Sept 2026)
    "": "Codex default for your plan (recommended)",
    "gpt-6-sol": "gpt-6-sol: strong all-rounder",
    "gpt-6-astra": "gpt-6-astra: complex research workflows",
    "gpt-6-luna": "gpt-6-luna: fastest, lightest on usage",
    "gpt-5.5": "gpt-5.5: legacy, retires Oct 14, 2026",
}
LANES = ["btc", "ai"]

if not is_owner():
    st.info("🔒 The Control Room is owner-only. Unlock with 🔑 Owner at the top right.", icon="🔒")
    st.stop()

settings = config.settings()
t_agents, t_settings, t_watch, t_voice, t_style, t_market, t_cal = st.tabs(
    ["🛰 Agents", "⚙️ Settings", "👀 Watchlist", "🗣 Voice & rules", "📚 Style library", "💹 Market inputs", "📅 Calendar"])

# ------------------------------------------------------------------ agents
with t_agents:
    auth = db.kv_get("codex_auth")
    checks = [
        ("Database", "Postgres (cloud)" if db.is_postgres() else "SQLite (local)", True),
        ("LLM", f"{env('LLM_BACKEND') or settings.get('llm', {}).get('backend', 'codex')} "
                f"(Codex login stored {auth['updated_at'][:10]})" if auth else "Codex login not uploaded yet", bool(auth)),
        ("Discord alerts", "webhook set" if env("DISCORD_WEBHOOK_URL") else "not set in this app's secrets",
         bool(env("DISCORD_WEBHOOK_URL"))),
        ("Instant runs", f"dispatch → {env('GITHUB_REPO')}" if env("GH_DISPATCH_TOKEN") else
         ("local background runs" if env("LOCAL_AGENT") == "1" else "queued until next scheduled run"),
         bool(env("GH_DISPATCH_TOKEN") or env("LOCAL_AGENT") == "1")),
        ("X handle", settings.get("account", {}).get("handle") or "set in Settings", bool(settings.get("account", {}).get("handle"))),
    ]
    c = st.columns(len(checks))
    for col, (name, val, ok) in zip(c, checks):
        col.markdown(f"{'🟢' if ok else '🟡'} **{name}**  \n<span class='xcp-muted'>{val}</span>", unsafe_allow_html=True)

    lim = settings.get("limits", {})
    st.caption(f"X API reads today: {x_reads_today()} / {lim.get('x_daily_post_cap', 900)} cap "
               f"(≈ \\${x_reads_today() * 0.005:.2f} at \\$0.005/post)")

    st.markdown("#### Run now")
    jobs = list(settings["slots"]) + ["nightly", "weekly", "snapshot", "style_refresh"]
    labels = {**{k: v.get("label", k) for k, v in settings["slots"].items()},
              "nightly": "🌙 Nightly (metrics + ideas)", "weekly": "📅 Weekly review", "snapshot": "💹 Market snapshot",
              "style_refresh": "📚 Style refresh"}
    cols = st.columns(len(jobs))
    for col, j in zip(cols, jobs):
        col.button(labels[j], key=f"run_{j}", width="stretch", on_click=enqueue, args=("run_job", {"job": j}))

    st.markdown("#### Recent runs")
    with db.session() as s:
        runs = list(s.scalars(select(db.Run).order_by(db.Run.started_at.desc()).limit(40)).all())
        reqs = list(s.scalars(select(db.Request).order_by(db.Request.created_at.desc()).limit(40)).all())
    icon = {"ok": "✅", "error": "❌", "running": "⏳"}
    for r in runs[:15]:
        with st.expander(f"{icon.get(r.status, '•')} {r.job} · {fmt_ny(r.started_at)} · {r.trigger}"
                         + (f" · X reads {r.x_reads}" if r.x_reads else "")):
            st.json(r.stats or {}, expanded=False)
            st.code(r.log or "(no log)", language=None)
    st.markdown("#### Request queue")
    if reqs:
        st.dataframe([{"id": r.id, "kind": r.kind, "status": r.status, "created": fmt_ny(r.created_at),
                       "error": r.error or ""} for r in reqs], hide_index=True, width="stretch")
    if st.button("🔔 Send a test Discord alert"):
        ok = notify.discord("✅ Test from X Control Panel", "If you can read this, alerts work.")
        st.toast("Sent" if ok else "Not sent: DISCORD_WEBHOOK_URL is missing in this app's secrets",
                 icon="🔔" if ok else "⚠️")

# ------------------------------------------------------------------ settings
with t_settings:
    new = copy.deepcopy(settings)
    c = st.columns(2)
    new["account"]["handle"] = c[0].text_input("Your X handle (no @)", settings["account"].get("handle", ""))
    new["account"]["premium"] = c[1].toggle("X Premium (long posts)", settings["account"].get("premium", True))

    st.markdown("**Mix targets (% of posts)**")
    tg = settings["targets"]["pillars"]
    c = st.columns(len(tg))
    for col, (g, v) in zip(c, tg.items()):
        new["targets"]["pillars"][g] = col.number_input(g, 0, 100, int(v), step=5)
    total = sum(new["targets"]["pillars"].values())
    if total != 100:
        st.warning(f"Targets sum to {total}%, not 100%")
    tt = settings["targets"]["tones"]
    c = st.columns(len(tt))
    for col, (g, v) in zip(c, tt.items()):
        new["targets"]["tones"][g] = col.number_input(f"tone: {g}", 0, 100, int(v), step=5)

    st.markdown("**Slots** (ET). `run_at` is when the agent starts; `post_at` is when you post.")
    slot_rows = [{"slot": k, "label": v.get("label", k), "run_at": v["run_at"], "post_at": v["post_at"],
                  "days": ",".join(v.get("days", [])), "lane": v.get("lane", "btc"), "stories": v.get("stories", 3),
                  "variants": v.get("variants", 3), "optional": bool(v.get("optional", False))}
                 for k, v in settings["slots"].items()]
    edited = st.data_editor(pd.DataFrame(slot_rows), hide_index=True, width="stretch", key="slots_ed",
                            disabled=["slot"],
                            column_config={"lane": st.column_config.SelectboxColumn("lane", options=LANES)})
    for _, row in edited.iterrows():
        new["slots"][row["slot"]].update({
            "label": row["label"], "run_at": row["run_at"], "post_at": row["post_at"],
            "days": [d.strip() for d in str(row["days"]).split(",") if d.strip()], "lane": row["lane"],
            "stories": int(row["stories"]), "variants": int(row["variants"]), "optional": bool(row["optional"])})
    st.caption("GitHub's cron triggers are set in .github/workflows/agent.yml. If you move a run_at by more "
               "than about an hour, update the cron there too.")

    st.markdown("**Showcase slots** (a Build Lab creation is the main post)")
    sc = st.data_editor(pd.DataFrame(settings.get("showcase", [])), hide_index=True, num_rows="dynamic",
                        width="stretch", key="sc_ed",
                        column_config={"day": st.column_config.SelectboxColumn("day", options=DAYS),
                                       "slot": st.column_config.SelectboxColumn("slot", options=list(settings["slots"]))})
    new["showcase"] = [{"day": r["day"], "slot": r["slot"]} for _, r in sc.iterrows() if r.get("day") and r.get("slot")]

    st.markdown("**Limits**")
    c = st.columns(3)
    new["limits"]["x_max_posts_per_run"] = c[0].number_input("X reads per run", 0, 5000, int(lim.get("x_max_posts_per_run", 300)))
    new["limits"]["x_daily_post_cap"] = c[1].number_input("X reads per day", 0, 20000, int(lim.get("x_daily_post_cap", 900)))
    new["limits"]["items_to_llm"] = c[2].number_input("Items shown to the writer", 20, 400, int(lim.get("items_to_llm", 120)))
    new["limits"]["include_watchlist_replies"] = st.toggle("Include watchlist accounts' replies",
                                                           bool(lim.get("include_watchlist_replies", False)))
    c = st.columns(3)
    new["llm"]["backend"] = c[0].selectbox("LLM backend", ["codex", "mock"],
                                           index=["codex", "mock"].index(settings["llm"].get("backend", "codex")))
    cur_model = settings["llm"].get("codex_model", "") or ""
    model_opts = list(CODEX_MODELS) + ([cur_model] if cur_model and cur_model not in CODEX_MODELS else [])
    new["llm"]["codex_model"] = c[1].selectbox(
        "Codex model (writes every draft)", model_opts, index=model_opts.index(cur_model),
        format_func=lambda m: CODEX_MODELS.get(m, f"{m} (custom)"),
        help="Uses your ChatGPT plan's Codex limits. Heavier models use more of your plan's allowance.")
    efforts = {"": "Model default", "low": "low: fastest", "medium": "medium", "high": "high: more careful writing",
               "xhigh": "xhigh: slowest, heaviest on limits"}
    cur_eff = settings["llm"].get("reasoning_effort", "") or ""
    eff_opts = list(efforts) + ([cur_eff] if cur_eff not in efforts else [])
    new["llm"]["reasoning_effort"] = c[2].selectbox("Reasoning effort", eff_opts, index=eff_opts.index(cur_eff),
                                                    format_func=lambda e: efforts.get(e, e))

    b = st.columns([1, 1, 4])
    if b[0].button("💾 Save settings", type="primary"):
        config.save("settings", new)
        st.toast("Settings saved", icon="💾")
        st.rerun()
    if b[1].button("Reset to defaults"):
        config.reset("settings")
        st.rerun()

# ------------------------------------------------------------------ watchlist
with t_watch:
    wl = config.get("watchlist")
    accounts = wl.get("accounts", [])
    st.caption(f"{len(accounts)} accounts. The Scout reads their recent posts at each slot "
               f"(bundled into as few X searches as possible).")
    pillar_opts = [""] + list(config.pillars())
    df = pd.DataFrame(accounts or [{"handle": "", "lane": "btc", "pillar": "", "note": ""}],
                      columns=["handle", "lane", "pillar", "note"])
    ed = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key="wl_ed",
                        column_config={"lane": st.column_config.SelectboxColumn("lane", options=LANES, required=True),
                                       "pillar": st.column_config.SelectboxColumn("pillar", options=pillar_opts)})
    with st.expander("Bulk add (paste handles)"):
        bulk = st.text_area("One per line, e.g. `@handle btc digital_credit`", height=120, key="wl_bulk")
    if st.button("💾 Save watchlist", type="primary"):
        rows = [{k: (v if isinstance(v, str) else "") for k, v in r.items()} for r in ed.to_dict("records")]
        for line in (bulk or "").splitlines():
            bits = line.replace(",", " ").split()
            if bits:
                rows.append({"handle": bits[0], "lane": bits[1] if len(bits) > 1 and bits[1] in LANES else "btc",
                             "pillar": bits[2] if len(bits) > 2 else "", "note": ""})
        seen, clean = set(), []
        for r in rows:
            h = (r.get("handle") or "").strip().lstrip("@")
            if h and h.lower() not in seen:
                seen.add(h.lower())
                clean.append({"handle": h, "lane": r.get("lane") or "btc", "pillar": r.get("pillar") or "",
                              "note": r.get("note") or ""})
        config.save("watchlist", {"accounts": clean})
        st.toast(f"Saved {len(clean)} accounts", icon="👀")
        st.rerun()

    st.markdown("**Keyword searches on X**")
    pcfg = config.get("pillars")
    xs = st.data_editor(pd.DataFrame(pcfg.get("x_searches", [])), num_rows="dynamic", hide_index=True,
                        width="stretch", key="xs_ed",
                        column_config={"lane": st.column_config.SelectboxColumn("lane", options=LANES),
                                       "query": st.column_config.TextColumn("query", width="large")})
    if st.button("💾 Save searches"):
        newp = copy.deepcopy(pcfg)
        newp["x_searches"] = [{"name": r["name"], "lane": r["lane"], "query": r["query"], "max": int(r["max"] or 30)}
                              for _, r in xs.iterrows() if r.get("query")]
        config.save("pillars", newp)
        st.toast("Searches saved", icon="🔎")

# ------------------------------------------------------------------ voice
with t_voice:
    st.markdown("**Posting guidelines** · every writer and rewrite follows these (guiding, not binding)")
    guidelines = st.text_area("Posting guidelines", config.get("guidelines"), height=230,
                              label_visibility="collapsed", key="guidelines_ta")
    if st.button("💾 Save guidelines", type="primary"):
        config.save("guidelines", guidelines)
        st.toast("Guidelines saved", icon="📏")
    st.divider()
    st.markdown("**Voice profile** · the single biggest lever on draft quality. Paste your best posts, phrases you "
                "never use, and what you believe.")
    voice = st.text_area("Voice profile", config.get("voice"), height=480, label_visibility="collapsed")
    if st.button("💾 Save voice", type="primary"):
        config.save("voice", voice)
        st.toast("Voice saved", icon="🗣")

# ------------------------------------------------------------------ market inputs
with t_market:
    m = settings.get("market", {})
    st.caption("Used for derived metrics (basic mNAV, effective yields). Update when Strategy/Strive announce changes.")
    c = st.columns(2)
    mstr = c[0].number_input("Strategy BTC holdings", 0, 5_000_000, int(m.get("mstr_btc_holdings") or 0), step=100)
    asst = c[1].number_input("Strive BTC holdings", 0, 1_000_000, int(m.get("asst_btc_holdings") or 0), step=10)
    c = st.columns(3)
    strc = c[0].number_input("STRC annual dividend rate %", 0.0, 30.0, float(m.get("strc_annual_rate_pct") or 0.0), step=0.25)
    sata = c[1].number_input("SATA annual dividend rate %", 0.0, 30.0, float(m.get("sata_annual_rate_pct") or 0.0), step=0.25)
    asof = c[2].text_input("Rates as of", m.get("rates_as_of", ""))
    tickers = st.text_input("Tickers on the Market Desk", ", ".join(m.get("tickers", [])))
    if st.button("💾 Save market inputs", type="primary"):
        new = copy.deepcopy(settings)
        new["market"].update({"mstr_btc_holdings": mstr or None, "asst_btc_holdings": asst or None,
                              "strc_annual_rate_pct": strc or None, "sata_annual_rate_pct": sata or None,
                              "rates_as_of": asof, "tickers": [t.strip() for t in tickers.split(",") if t.strip()]})
        config.save("settings", new)
        st.toast("Saved. Takes effect on the next snapshot.", icon="💹")

# ------------------------------------------------------------------ calendar
with t_cal:
    st.caption("Events the writer should know about: FOMC, CPI, jobs, earnings, STRC rate announcements, "
               "SATA rate changes, launches.")
    with db.session() as s:
        evs = list(s.scalars(select(db.CalendarEvent).where(db.CalendarEvent.date >= today_ny().isoformat())
                             .order_by(db.CalendarEvent.date)).all())
    df = pd.DataFrame([{"id": e.id, "date": e.date, "time": e.time, "title": e.title, "pillar": e.pillar,
                        "importance": e.importance, "notes": e.notes} for e in evs],
                      columns=["id", "date", "time", "title", "pillar", "importance", "notes"])
    ed = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key="cal_ed",
                        disabled=["id"],
                        column_config={"pillar": st.column_config.SelectboxColumn("pillar", options=list(config.pillars())),
                                       "importance": st.column_config.NumberColumn("importance", min_value=1, max_value=3)})
    if st.button("💾 Save calendar", type="primary"):
        keep_ids = set()
        with db.session() as s:
            for r in ed.to_dict("records"):
                if not r.get("date") or not r.get("title"):
                    continue
                rid = r.get("id")
                row = s.get(db.CalendarEvent, int(rid)) if rid and not pd.isna(rid) else None
                if row is None:
                    row = db.CalendarEvent()
                    s.add(row)
                row.date, row.time, row.title = str(r["date"])[:10], str(r.get("time") or "")[:5], r["title"]
                row.pillar = r.get("pillar") or "macro"
                row.importance = int(r.get("importance") or 2)
                row.notes = r.get("notes") or ""
                s.flush()
                keep_ids.add(row.id)
            for e in evs:
                if e.id not in keep_ids:
                    s.delete(s.get(db.CalendarEvent, e.id))
            s.commit()
        st.toast("Calendar saved", icon="📅")
        st.rerun()

# ------------------------------------------------------------------ style library
FORMATS = ["short_observation", "quick_analysis", "long_analysis", "thread", "humor_meme", "contrarian",
           "data_callout", "news_reaction", "question_hook", "chart_callout"]
with t_style:
    sc = settings.get("style", {})
    st.info(f"📚 **Style refresh** (Agents tab → Run now) pulls the latest {sc.get('posts_per_account', 100)} posts from "
            f"each watchlist account plus up to {sc.get('own_posts_max', 1000)} of yours via the X API "
            "(≈ \\$0.005/post), then extracts new patterns. Mark favorites by setting strength 8+; "
            f"favorites are always shown to the writer, and the rest rotate.", icon="📚")
    with db.session() as s:
        rows = list(s.scalars(select(db.StyleExample).order_by(db.StyleExample.source.desc(),
                                                                db.StyleExample.strength.desc())).all())
    mine_n = sum(1 for r in rows if r.source == "mine")
    st.caption(f"{mine_n} of your own posts · {len(rows) - mine_n} craft patterns from accounts you admire · "
               f"{sum(1 for r in rows if r.active)} active. The writer sees your posts first, then a varied, "
               "mostly-short sample of patterns on every run. Other accounts' wording is never stored.")

    with st.expander("➕ Add your own best posts (highest priority)"):
        with st.form("add_mine", clear_on_submit=True):
            txt = st.text_area("Paste posts. Separate several with a line containing only ---", height=180)
            c = st.columns(3)
            fmt = c[0].selectbox("Format", FORMATS)
            pil = c[1].selectbox("Pillar", list(config.pillars()))
            url = c[2].text_input("Link (optional)")
            if st.form_submit_button("Add") and txt.strip():
                from xcp import xtext
                with db.session() as s:
                    for post in xtext.split_parts(txt):
                        s.add(db.StyleExample(source="mine", text=post, format=fmt, pillar=pil, url=url,
                                              length="long" if len(post) > 600 else "short", strength=9))
                    s.commit()
                st.rerun()

    c = st.columns(3)
    f_src = c[0].multiselect("Source", ["mine", "admired"], default=["mine", "admired"])
    f_fmt = c[1].multiselect("Format", FORMATS, placeholder="All formats")
    f_handle = c[2].multiselect("Account", sorted({r.handle for r in rows if r.handle}), placeholder="All accounts")
    shown = [r for r in rows if r.source in f_src and (not f_fmt or r.format in f_fmt)
             and (not f_handle or r.handle in f_handle)]
    df = pd.DataFrame([{"id": r.id, "active": r.active, "strength": r.strength, "source": r.source,
                        "handle": r.handle, "format": r.format, "length": r.length, "pillar": r.pillar,
                        "hook": r.hook_type, "pattern": r.pattern, "template": r.skeleton,
                        "your-lane example": r.demo if r.source != "mine" else r.text, "link": r.url}
                       for r in shown],
                      columns=["id", "active", "strength", "source", "handle", "format", "length", "pillar", "hook",
                               "pattern", "template", "your-lane example", "link"])
    ed = st.data_editor(df, hide_index=True, width="stretch", key="style_ed", disabled=["id", "source", "handle"],
                        column_config={
                            "strength": st.column_config.NumberColumn("strength", min_value=1, max_value=10),
                            "format": st.column_config.SelectboxColumn("format", options=FORMATS),
                            "length": st.column_config.SelectboxColumn("length", options=["short", "medium", "long"]),
                            "pillar": st.column_config.SelectboxColumn("pillar", options=list(config.pillars())),
                            "pattern": st.column_config.TextColumn("pattern", width="large"),
                            "template": st.column_config.TextColumn("template", width="large"),
                            "your-lane example": st.column_config.TextColumn("your-lane example", width="large"),
                            "link": st.column_config.LinkColumn("link", display_text="source ↗")})
    if st.button("💾 Save library changes", type="primary"):
        with db.session() as s:
            for r in ed.to_dict("records"):
                row = s.get(db.StyleExample, int(r["id"]))
                if row is None:
                    continue
                row.active, row.strength = bool(r["active"]), int(r["strength"] or 7)
                row.format, row.length, row.pillar = r["format"], r["length"], r["pillar"]
                row.hook_type, row.pattern, row.skeleton = r["hook"] or "", r["pattern"] or "", r["template"] or ""
                if row.source == "mine":
                    row.text = r["your-lane example"] or row.text
                else:
                    row.demo = r["your-lane example"] or ""
            s.commit()
        st.toast("Style library saved", icon="📚")
        st.rerun()
