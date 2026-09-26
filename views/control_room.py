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
LANES = ["btc", "ai"]

if not is_owner():
    st.info("🔒 The Control Room is owner-only. Unlock with 🔑 Owner at the top right.", icon="🔒")
    st.stop()

settings = config.settings()
t_agents, t_settings, t_watch, t_voice, t_market, t_cal = st.tabs(
    ["🛰 Agents", "⚙️ Settings", "👀 Watchlist", "🗣 Voice", "💹 Market inputs", "📅 Calendar"])

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
    jobs = list(settings["slots"]) + ["nightly", "weekly", "snapshot"]
    labels = {**{k: v.get("label", k) for k, v in settings["slots"].items()},
              "nightly": "🌙 Nightly (metrics + ideas)", "weekly": "📅 Weekly review", "snapshot": "💹 Market snapshot"}
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
    c = st.columns(2)
    new["llm"]["backend"] = c[0].selectbox("LLM backend", ["codex", "mock"],
                                           index=["codex", "mock"].index(settings["llm"].get("backend", "codex")))
    new["llm"]["codex_model"] = c[1].text_input("Codex model (blank = default)", settings["llm"].get("codex_model", ""))

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
    st.caption("The single biggest lever on draft quality. Paste your best posts, phrases you never use, "
               "and what you believe.")
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
